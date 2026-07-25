from django.http import JsonResponse


def assetlinks(request):
    return JsonResponse(
        [
            {
                "relation": [
                    "delegate_permission/common.handle_all_urls"
                ],
                "target": {
                    "namespace": "android_app",
                    "package_name": "br.com.flexihub.app",
                    "sha256_cert_fingerprints": [
                        "54:84:7F:1E:66:3D:34:D0:FD:E6:E7:83:9F:37:35:6E:13:12:27:FE:E0:B9:B3:F3:47:5A:67:F4:B9:AF:F1:12"
                    ],
                },
            }
        ],
        safe=False,
    )