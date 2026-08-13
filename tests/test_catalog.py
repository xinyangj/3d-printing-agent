from __future__ import annotations

import io

import httpx
from PIL import Image

from printing_agent.catalogs import ThingiverseCatalog, sanitize_catalog_text
from printing_agent.config import Settings


def _image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "#44aa99").save(output, format="PNG")
    return output.getvalue()


async def test_thingiverse_search_and_page_gallery_are_normalized(
    settings: Settings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/search/"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 42,
                        "name": "Headphone hook",
                        "description": "<p>Wall mounted hook</p>",
                        "creator": {"name": "maker"},
                        "license": "CC BY",
                    }
                ],
            )
        if path == "/things/42":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "name": "Headphone hook",
                    "description": "<p>Printed wall hook</p>",
                    "instructions": "<b>Use two screws</b>",
                    "creator": {"name": "maker"},
                    "license": "CC BY",
                    "public_url": "https://www.thingiverse.com/thing:42",
                },
            )
        if path == "/things/42/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 9,
                        "name": "hook.stl",
                        "extension": "stl",
                        "size": 100,
                        "download_url": "https://api.thingiverse.com/files/9/download",
                    }
                ],
            )
        if path == "/things/42/images":
            return httpx.Response(
                200,
                json=[
                    {
                        "sizes": [
                            {
                                "url": "https://cdn.thingiverse.com/hook.png",
                                "width": 32,
                                "height": 24,
                            }
                        ]
                    }
                ],
            )
        if request.url.host == "cdn.thingiverse.com":
            assert "authorization" not in request.headers
            return httpx.Response(
                200,
                content=_image_bytes(),
                headers={"content-type": "image/png"},
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.AsyncClient(
        base_url=settings.thingiverse_api_url,
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    catalog = ThingiverseCatalog(settings, client)
    candidates = await catalog.search("headphone hook", 1, 10)
    inspection, images = await catalog.inspect_page_with_images(
        "workflow",
        "round",
        candidates[0].id,
    )

    assert candidates[0].title == "Headphone hook"
    assert inspection.candidate.files[0].id == "9"
    assert inspection.candidate.instructions == "Use two screws"
    assert len(images) == 1
    assert images[0].mime_type == "image/jpeg"
    await catalog.close()


def test_catalog_html_is_reduced_to_bounded_plain_text() -> None:
    assert sanitize_catalog_text("<p>Hello <b>world</b></p>") == "Hello world"
