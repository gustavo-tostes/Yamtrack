import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

base_url = "https://openlibrary.org/api"
search_url = "https://openlibrary.org/search.json"
headers = {"User-Agent": "FlexiHub/1.0"}

OPENLIBRARY_LANG = "pt"
OPENLIBRARY_LANGUAGE_CODES = {
    "por",
    "pt",
    "/languages/por",
    "/languages/pt",
    "portuguese",
    "português",
}

NO_SYNOPSIS = "Sinopse não disponível."

PHYSICAL_FORMAT_TRANSLATIONS = {
    "Hardcover": "Capa dura",
    "Paperback": "Capa comum",
    "Mass Market Paperback": "Livro de bolso",
    "Ebook": "E-book",
    "E-Book": "E-book",
    "Audio Cassette": "Audiocassete",
    "Audio CD": "Audiolivro em CD",
    "Board Book": "Livro cartonado",
    "Library Binding": "Encadernação de biblioteca",
    "Spiral-Bound": "Espiral",
    "Unknown": "Desconhecido",
}

SUBJECT_TRANSLATIONS = {
    "Fiction": "Ficção",
    "Juvenile Fiction": "Ficção juvenil",
    "Young Adult Fiction": "Ficção jovem adulta",
    "Fantasy": "Fantasia",
    "Science Fiction": "Ficção científica",
    "Science Fiction & Fantasy": "Ficção científica e fantasia",
    "Magic": "Magia",
    "Adventure": "Aventura",
    "Classics": "Clássicos",
    "Mystery": "Mistério",
    "Romance": "Romance",
    "Horror": "Terror",
    "Thriller": "Suspense",
    "Biography": "Biografia",
    "History": "História",
    "Children's stories": "Histórias infantis",
    "Children's fiction": "Ficção infantil",
    "Literature": "Literatura",
    "Comics & Graphic Novels": "Quadrinhos e graphic novels",
}


def handle_error(error):
    """Handle Open Library API errors."""
    raise services.ProviderAPIError(
        Sources.OPENLIBRARY.value,
        error,
    )


def search(query, page):
    """Search for books on Open Library, prioritizing Portuguese editions."""
    cache_key = (
        f"search_{Sources.OPENLIBRARY.value}_{MediaTypes.BOOK.value}_"
        f"{OPENLIBRARY_LANG}_{query}_{page}"
    )
    data = cache.get(cache_key)

    if data is None:
        params = {
            "q": query,
            "fields": (
                "title,key,editions,"
                "editions.key,editions.cover_i,editions.title,editions.language"
            ),
            "limit": settings.PER_PAGE,
            "page": page,
            "lang": OPENLIBRARY_LANG,
        }

        try:
            response = services.api_request(
                Sources.OPENLIBRARY.value,
                "GET",
                search_url,
                params=params,
                headers=headers,
            )
        except requests.RequestException as e:
            handle_error(e)

        results = []
        for doc in response.get("docs", []):
            edition_docs = doc.get("editions", {}).get("docs", [])
            if edition_docs == []:
                continue

            top_edition = pick_preferred_edition(edition_docs)
            media_id = extract_openlibrary_id(top_edition.get("key"))
            title = doc.get("title")
            edition_title = top_edition.get("title") or title

            if not media_id or not title:
                continue

            if edition_title != title:
                result_title = f"{edition_title}: {title}"
            else:
                result_title = title

            results.append(
                {
                    "media_id": media_id,
                    "source": Sources.OPENLIBRARY.value,
                    "media_type": MediaTypes.BOOK.value,
                    "title": result_title,
                    "image": get_image_url(top_edition),
                },
            )

        total_results = response["numFound"]
        data = helpers.format_search_response(
            page,
            settings.PER_PAGE,
            total_results,
            results,
        )

        cache.set(cache_key, data)
    return data


def pick_preferred_edition(editions):
    """Pick a Portuguese edition when Open Library provides one."""
    for edition in editions:
        if edition_has_portuguese_language(edition):
            return edition
    return editions[0]


def edition_has_portuguese_language(edition):
    """Return True when an edition looks like it is in Portuguese."""
    languages = edition.get("language", [])

    if not languages:
        return False

    for language in languages:
        if isinstance(language, dict):
            language_value = (
                language.get("key")
                or language.get("name")
                or language.get("code")
                or ""
            )
        else:
            language_value = str(language)

        if language_value.strip().lower() in OPENLIBRARY_LANGUAGE_CODES:
            return True

    return False


def extract_openlibrary_id(path):
    """
    Extract the ID from an OpenLibrary path.

    Args:
        path (str): A path like '/works/OL123W'

    Returns:
        str: The extracted ID (e.g., 'OL123W')
    """
    if not path:
        return None

    # Handle both full URLs and path fragments
    return path.rstrip("/").split("/")[-1]


def get_image_url(doc):
    """Get the cover image URL for a book."""
    try:
        cover_id = doc["cover_i"]
        if cover_id:
            return f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"

    except KeyError:
        return settings.IMG_NONE

    return settings.IMG_NONE


def book(media_id):
    """Get metadata for a book from Open Library."""
    return asyncio.run(async_book(media_id))


async def async_book(media_id):
    """Asynchronous implementation of book metadata retrieval."""
    cache_key = (
        f"{Sources.OPENLIBRARY.value}_{MediaTypes.BOOK.value}_"
        f"{OPENLIBRARY_LANG}_{media_id}"
    )
    data = cache.get(cache_key)

    if data is None:
        book_url = f"https://openlibrary.org/books/{media_id}.json"

        try:
            response_book = services.api_request(
                Sources.OPENLIBRARY.value,
                "GET",
                book_url,
                headers=headers,
            )
        except requests.RequestException as e:
            handle_error(e)

        works = response_book.get("works", [])
        if works:
            work = works[0]
            work_id = extract_openlibrary_id(work["key"])
            work_url = f"https://openlibrary.org/works/{work_id}.json"

            try:
                response_work = services.api_request(
                    Sources.OPENLIBRARY.value,
                    "GET",
                    work_url,
                    headers=headers,
                )
            except requests.RequestException as e:
                handle_error(e)
        else:
            response_work = {}

        # Run authors, editions, and ratings concurrently
        authors_task = asyncio.create_task(
            get_authors(response_work),
        )
        editions_task = asyncio.create_task(
            get_editions(response_book, response_work),
        )
        ratings_task = asyncio.create_task(
            get_ratings(response_work),
        )
        score, score_count = await ratings_task

        data = {
            "media_id": media_id,
            "source": Sources.OPENLIBRARY.value,
            "source_url": f"https://openlibrary.org/books/{media_id}",
            "media_type": MediaTypes.BOOK.value,
            "title": response_book["title"],
            "max_progress": response_book.get("number_of_pages"),
            "image": get_cover_image_url(response_book),
            "synopsis": get_description(response_book, response_work),
            "genres": get_subjects(response_work),
            "score": score,
            "score_count": score_count,
            "details": {
                "physical_format": get_physical_format(response_book),
                "number_of_pages": response_book.get("number_of_pages"),
                "publish_date": get_publish_date(response_book),
                "author": await authors_task,
                "publishers": get_publishers(response_book),
                "isbn": get_isbns(response_book),
            },
            "related": {
                "other_editions": await editions_task,
            },
        }

        cache.set(cache_key, data)

    return data


def get_cover_image_url(response):
    """Get the cover image URL from a work response."""
    covers = response.get("covers", [])
    if covers:
        return f"https://covers.openlibrary.org/b/id/{covers[0]}-L.jpg"
    return settings.IMG_NONE


def get_description(response_book, response_work):
    """Extract and clean up the book description."""
    if "description" in response_book:
        description = response_book["description"]
    elif "description" in response_work:
        description = response_work["description"]
    else:
        description = NO_SYNOPSIS

    # sometimes the description is a dict
    # like {'type': '/type/text', 'value': '...'}
    if isinstance(description, dict):
        description = description["value"]

    if description != NO_SYNOPSIS:
        soup = BeautifulSoup(description, "html.parser")
        text = soup.get_text(separator=" ")
        description = " ".join(text.split())

    return description


def get_physical_format(response):
    """Get the physical format of the book."""
    format_value = response.get("physical_format")
    if format_value:
        normalized = format_value.title()
        return PHYSICAL_FORMAT_TRANSLATIONS.get(normalized, normalized)
    return None


def get_publish_date(response):
    """Get the first publication date."""
    if "publish_date" in response:
        publish_date = response["publish_date"].removeprefix("cop. ")

        date_formats = [
            "%B %d, %Y",  # January 19, 2001
            "%b %d, %Y",  # Oct 01, 2017
            "%d %B %Y",  # 18 March 2025
        ]
        for date_format in date_formats:
            try:
                parsed_date = datetime.strptime(publish_date, date_format).replace(
                    tzinfo=ZoneInfo("UTC"),
                )
                return parsed_date.strftime("%Y-%m-%d")
            except ValueError:
                continue
        # If no format matches, return the original string
        return publish_date
    return None


async def get_authors(response):
    """Get list of author names asynchronously."""
    authors = []
    author_entries = response.get("authors", [])

    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = []
        for author in author_entries:
            if isinstance(author, dict) and "author" in author:
                author_key = author["author"]["key"]
                author_url = f"https://openlibrary.org{author_key}.json"
                tasks.append(fetch_author_data(session, author_url))

        author_data_list = await asyncio.gather(*tasks)
        authors = [
            data.get("name", "Autor desconhecido") for data in author_data_list if data
        ]

    return authors or None


async def fetch_author_data(session, url):
    """Fetch author data asynchronously."""
    async with session.get(url) as response:
        if response.status == requests.codes.ok:
            return await response.json()

    return None


def get_subjects(response):
    """Get list of subjects/genres."""
    if "subjects" in response:
        subjects = response["subjects"][:5]
        return [
            SUBJECT_TRANSLATIONS.get(subject, subject)
            for subject in subjects
        ]
    return None


def get_publishers(response):
    """Get list of publishers."""
    if "publishers" in response:
        return response.get("publishers", [])[:5]
    return None


def get_isbns(response):
    """Get list of ISBNs."""
    isbn_13 = response.get("isbn_13", [])
    isbn_10 = response.get("isbn_10", [])
    isbns = isbn_13 + isbn_10
    if isbns:
        return isbns
    return None


async def get_editions(response_book, response_work):
    """Get list of editions asynchronously, prioritizing Portuguese editions."""
    book_id = extract_openlibrary_id(response_book.get("key", ""))
    work_id = extract_openlibrary_id(response_work.get("key", ""))

    if not work_id:
        work_id = book_id

    # limit to 500 editions, pagination is not supported
    url = f"https://openlibrary.org/works/{work_id}/editions.json?limit=500"

    async with (
        aiohttp.ClientSession(headers=headers) as session,
        session.get(url) as response,
    ):
        if response.status == requests.codes.ok:
            data = await response.json()
            entries = data.get("entries", [])

            filtered_entries = [
                edition
                for edition in entries
                if extract_openlibrary_id(edition.get("key")) != book_id
                and edition.get("title")
            ]

            filtered_entries.sort(
                key=lambda edition: (
                    0 if edition_has_portuguese_language(edition) else 1,
                    edition.get("title") or "",
                ),
            )

            return [
                {
                    "source": Sources.OPENLIBRARY.value,
                    "source_url": (
                        "https://openlibrary.org/books/"
                        f"{extract_openlibrary_id(edition['key'])}"
                    ),
                    "media_id": extract_openlibrary_id(edition["key"]),
                    "media_type": MediaTypes.BOOK.value,
                    "title": edition.get("title"),
                    "image": get_cover_image_url(edition),
                }
                for edition in filtered_entries
            ]
    return []


async def get_ratings(response_work):
    """Get ratings data for a book asynchronously."""
    work_id = extract_openlibrary_id(response_work.get("key", ""))

    if not work_id:
        return None, None

    url = f"https://openlibrary.org/works/{work_id}/ratings.json"

    async with (
        aiohttp.ClientSession(headers=headers) as session,
        session.get(url) as response,
    ):
        if response.status == requests.codes.ok:
            data = await response.json()
            summary = data.get("summary", {})
            average = summary.get("average")
            count = summary.get("count")

            if average and count:
                # Convert to 10-point scale (multiply by 2) and round to 1 decimal place
                score = round(summary["average"] * 2, 1)
                score_count = summary["count"]
                return score, score_count

    return None, 0