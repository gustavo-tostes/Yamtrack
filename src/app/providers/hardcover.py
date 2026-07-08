import logging

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

base_url = "https://api.hardcover.app/v1/graphql"
MAX_SEARCH_QUERY_LENGTH = 50

HARDCOVER_LOCALE = "pt-BR"
NO_SYNOPSIS = "Sinopse não disponível."

FORMAT_TRANSLATIONS = {
    "Unknown": "Desconhecido",
    "Hardcover": "Capa dura",
    "Paperback": "Capa comum",
    "Mass Market Paperback": "Livro de bolso",
    "Ebook": "E-book",
    "E-Book": "E-book",
    "Digital": "Digital",
    "Kindle Edition": "Edição Kindle",
    "Audiobook": "Audiolivro",
    "Audio": "Áudio",
    "Audio Cd": "Audiolivro em CD",
    "Audio CD": "Audiolivro em CD",
    "Board Book": "Livro cartonado",
    "Library Binding": "Encadernação de biblioteca",
    "Spiral-Bound": "Espiral",
    "Leather Bound": "Capa de couro",
    "Box Set": "Box",
    "Graphic Novel": "Graphic novel",
    "Comic": "Quadrinho",
    "Manga": "Mangá",
}

TAG_TRANSLATIONS = {
    "Action": "Ação",
    "Adventure": "Aventura",
    "Adult": "Adulto",
    "Anthologies": "Antologias",
    "Art": "Arte",
    "Biography": "Biografia",
    "Business": "Negócios",
    "Children": "Infantil",
    "Children's": "Infantil",
    "Classics": "Clássicos",
    "Comedy": "Comédia",
    "Comic Book": "Quadrinho",
    "Comics": "Quadrinhos",
    "Comics & Graphic Novels": "Quadrinhos e graphic novels",
    "Contemporary": "Contemporâneo",
    "Crime": "Crime",
    "Drama": "Drama",
    "Education": "Educação",
    "Essays": "Ensaios",
    "Family": "Família",
    "Fantasy": "Fantasia",
    "Fiction": "Ficção",
    "Food": "Culinária",
    "Graphic Novels": "Graphic novels",
    "Historical": "Histórico",
    "Historical Fiction": "Ficção histórica",
    "History": "História",
    "Horror": "Terror",
    "Humor": "Humor",
    "Juvenile Fiction": "Ficção juvenil",
    "LGBT": "LGBT",
    "LGBTQ": "LGBTQ",
    "Literary Fiction": "Ficção literária",
    "Literature": "Literatura",
    "Magic": "Magia",
    "Manga": "Mangá",
    "Memoir": "Memórias",
    "Middle Grade": "Infantojuvenil",
    "Mystery": "Mistério",
    "Nonfiction": "Não ficção",
    "Paranormal": "Paranormal",
    "Philosophy": "Filosofia",
    "Poetry": "Poesia",
    "Politics": "Política",
    "Psychology": "Psicologia",
    "Queer": "Queer",
    "Religion": "Religião",
    "Romance": "Romance",
    "Science": "Ciência",
    "Science Fiction": "Ficção científica",
    "Science Fiction & Fantasy": "Ficção científica e fantasia",
    "Self Help": "Autoajuda",
    "Short Stories": "Contos",
    "Suspense": "Suspense",
    "Thriller": "Suspense",
    "Travel": "Viagem",
    "True Crime": "Crime real",
    "Urban Fantasy": "Fantasia urbana",
    "War": "Guerra",
    "Young Adult": "Jovem adulto",
    "Young Adult Fiction": "Ficção jovem adulta",
}


def cap_search_query(query):
    """Limit long book queries before sending them to Hardcover search."""
    query = str(query or "")

    if len(query) <= MAX_SEARCH_QUERY_LENGTH:
        return query

    capped_query = query[:MAX_SEARCH_QUERY_LENGTH]
    if query[MAX_SEARCH_QUERY_LENGTH].isspace():
        return capped_query.rstrip()

    word_boundary = capped_query.rfind(" ")
    if word_boundary == -1:
        return capped_query

    return capped_query[:word_boundary].rstrip()


def handle_error(error):
    """Handle Hardcover API errors."""
    error_resp = error.response
    status_code = error_resp.status_code

    try:
        error_json = error_resp.json()
    except requests.exceptions.JSONDecodeError as json_error:
        logger.exception("Failed to decode JSON response")
        raise services.ProviderAPIError(Sources.HARDCOVER.value, error) from json_error

    if status_code == requests.codes.unauthorized:
        details = error_json["error"]
        raise services.ProviderAPIError(Sources.HARDCOVER.value, error, details)

    raise services.ProviderAPIError(Sources.HARDCOVER.value, error)


def search(query, page):
    """Search for books on Hardcover."""
    query = cap_search_query(query)
    cache_key = (
        f"search_{Sources.HARDCOVER.value}_{MediaTypes.BOOK.value}_"
        f"{HARDCOVER_LOCALE}_{query}_{page}"
    )
    data = cache.get(cache_key)

    if data is None:
        search_query = """
        query SearchBooks($query: String!, $per_page: Int!, $page: Int!) {
          search(
            query: $query,
            query_type: "Book",
            per_page: $per_page,
            page: $page,
          ) {
            results
          }
        }
        """

        variables = {
            "query": query,
            "per_page": settings.PER_PAGE,
            "page": page,
        }

        try:
            response = services.api_request(
                Sources.HARDCOVER.value,
                "POST",
                base_url,
                params={"query": search_query, "variables": variables},
                headers={"Authorization": settings.HARDCOVER_API},
            )
        except requests.exceptions.HTTPError as error:
            response = handle_error(error)

        hits = response["data"]["search"]["results"]["hits"]
        results = [
            {
                "media_id": hit["document"]["id"],
                "source": Sources.HARDCOVER.value,
                "media_type": MediaTypes.BOOK.value,
                "title": hit["document"]["title"],
                "image": get_image_url(hit["document"]),
            }
            for hit in hits
        ]
        total_results = response["data"]["search"]["results"]["found"]

        data = helpers.format_search_response(
            page,
            settings.PER_PAGE,
            total_results,
            results,
        )

        cache.set(cache_key, data)

    return data


def book(media_id):
    """Get metadata for a book from Hardcover."""
    cache_key = (
        f"{Sources.HARDCOVER.value}_{MediaTypes.BOOK.value}_"
        f"{HARDCOVER_LOCALE}_{media_id}"
    )
    data = cache.get(cache_key)

    if data is None:
        book_query = """
        query GetBookDetails($book_id: Int!) {
          books_by_pk(id: $book_id) {
            id
            title
            cached_image(path: "url")
            description
            cached_tags(path: "Genre")
            rating
            ratings_count
            pages
            release_date
            slug
            cached_contributors(path: "[0]['author']['name']")
            default_cover_edition {
              edition_format
              isbn_13
              isbn_10
              release_date
              publisher {
                name
              }
            }
          }
        }
        """

        variables = {
            "book_id": int(media_id),
        }

        try:
            response = services.api_request(
                Sources.HARDCOVER.value,
                "POST",
                base_url,
                params={"query": book_query, "variables": variables},
                headers={"Authorization": settings.HARDCOVER_API},
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        book_data = response["data"]["books_by_pk"]

        if not book_data:
            services.raise_not_found_error(
                Sources.HARDCOVER.value,
                media_id,
                "book",
            )

        edition_details = get_edition_details(book_data.get("default_cover_edition"))

        data = {
            "media_id": book_data["id"],
            "source": Sources.HARDCOVER.value,
            "source_url": f"https://hardcover.app/books/{book_data['slug']}",
            "media_type": MediaTypes.BOOK.value,
            "title": book_data["title"],
            "max_progress": book_data.get("pages"),
            "image": book_data.get("cached_image") or settings.IMG_NONE,
            "synopsis": get_description(book_data.get("description")),
            "genres": get_tags(book_data.get("cached_tags")),
            "score": get_ratings(book_data.get("rating")),
            "score_count": book_data.get("ratings_count", 0),
            "details": {
                "format": edition_details.get("format"),
                "number_of_pages": book_data.get("pages"),
                "publish_date": edition_details.get("release_date")
                or book_data.get("release_date"),
                "author": book_data.get("cached_contributors"),
                "publisher": edition_details.get("publisher"),
                "isbn": edition_details.get("isbn"),
            },
        }

        cache.set(cache_key, data)

    return data


def get_description(description):
    """Clean and normalize the book description."""
    if not description:
        return NO_SYNOPSIS

    soup = BeautifulSoup(description, "html.parser")
    text = soup.get_text(separator=" ")
    text = " ".join(text.split())

    if not text:
        return NO_SYNOPSIS

    if text.strip() == "No synopsis available.":
        return NO_SYNOPSIS

    return text


def translate_format(format_value):
    """Translate known Hardcover edition formats to Brazilian Portuguese."""
    if not format_value:
        return None

    normalized = str(format_value).strip()
    title_case = normalized.title()

    return (
        FORMAT_TRANSLATIONS.get(normalized)
        or FORMAT_TRANSLATIONS.get(title_case)
        or title_case
    )


def translate_tag(tag):
    """Translate known book tags/genres to Brazilian Portuguese."""
    if not tag:
        return tag

    normalized = str(tag).strip()
    title_case = normalized.title()

    return (
        TAG_TRANSLATIONS.get(normalized)
        or TAG_TRANSLATIONS.get(title_case)
        or normalized
    )


def get_tags(tags_data):
    """Get processed tags/genres from API data."""
    if not tags_data:
        return None

    translated_tags = []

    for tag_data in tags_data:
        if isinstance(tag_data, dict):
            tag = tag_data.get("tag")
        else:
            tag = tag_data

        if tag:
            translated_tags.append(translate_tag(tag))

    return translated_tags or None


def get_ratings(rating_data):
    """Get processed rating from API data."""
    if not rating_data:
        return None
    return round(float(rating_data) * 2, 1)


def get_edition_details(edition_data):
    """Get processed edition details from API data."""
    if not edition_data:
        return {}

    isbns = []
    if edition_data.get("isbn_10"):
        isbns.append(edition_data["isbn_10"])
    if edition_data.get("isbn_13"):
        isbns.append(edition_data["isbn_13"])

    publisher_name = None
    if edition_data.get("publisher"):
        publisher_name = edition_data["publisher"].get("name")

    return {
        "format": translate_format(edition_data.get("edition_format") or "Unknown"),
        "publisher": publisher_name,
        "isbn": isbns or None,
        "release_date": edition_data.get("release_date"),
    }


def get_image_url(response):
    """Get the cover image URL for a book."""
    if response.get("image") and response["image"].get("url"):
        return response["image"]["url"]
    return settings.IMG_NONE