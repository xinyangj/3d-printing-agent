from __future__ import annotations

import asyncio
import hashlib
import html
import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from PIL import Image, UnidentifiedImageError

from printing_agent.config import Settings
from printing_agent.domain import (
    CandidateFile,
    CandidatePageInspection,
    ModelCandidate,
)
from printing_agent.errors import ConfigurationError, ExternalServiceError, PolicyViolationError

_SOURCE_FORMATS = {"3mf", "scad", "stl", "step", "stp"}
_DERIVATIVE_BLOCKING_LICENSES = {"cc-by-nd", "creative commons - attribution - no derivatives"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def sanitize_catalog_text(value: str | None, limit: int = 20_000) -> str:
    parser = _TextExtractor()
    parser.feed(html.unescape(value or ""))
    normalized = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return normalized[:limit]


@dataclass(frozen=True)
class CatalogImage:
    digest: str
    mime_type: str
    data: bytes


class ThingiverseCatalog:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        api_host = urlparse(settings.thingiverse_api_url).hostname
        self._allowed_hosts = {
            host
            for host in (
                api_host,
                "cdn.thingiverse.com",
                "thingiverse-production-new.s3.amazonaws.com",
                "thingiverse-production.s3.amazonaws.com",
            )
            if host
        }

    async def _http(self) -> httpx.AsyncClient:
        if not self.settings.thingiverse_token:
            raise ConfigurationError(
                "PRINTING_AGENT_THINGIVERSE_TOKEN is required for catalog discovery"
            )
        if self._client is not None:
            return self._client
        self._client = httpx.AsyncClient(
            base_url=self.settings.thingiverse_api_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "printing-agent/0.1",
            },
            follow_redirects=True,
            timeout=httpx.Timeout(30),
        )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str, page: int, limit: int) -> list[ModelCandidate]:
        client = await self._http()
        try:
            response = await client.get(
                f"/search/{quote(query, safe='')}/",
                params={"type": "things", "page": page, "per_page": limit},
                headers=self._auth_headers(None),
            )
            self._raise_for_status(response, "search Thingiverse")
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExternalServiceError(f"Thingiverse search failed: {exc}") from exc

        raw_results = (
            payload.get("hits", payload.get("results", []))
            if isinstance(payload, dict)
            else payload
        )
        if not isinstance(raw_results, list):
            raise ExternalServiceError("Thingiverse returned an invalid search response")
        candidates = [self._candidate_from_json(item) for item in raw_results[:limit]]
        return [candidate for candidate in candidates if candidate is not None]

    async def inspect_page_with_images(
        self,
        workflow_id: str,
        search_round_id: str,
        candidate_id: str,
    ) -> tuple[CandidatePageInspection, list[CatalogImage]]:
        client = await self._http()
        try:
            thing_response, files_response, images_response = await asyncio.gather(
                client.get(
                    f"/things/{candidate_id}",
                    headers=self._auth_headers(None),
                ),
                client.get(
                    f"/things/{candidate_id}/files",
                    headers=self._auth_headers(None),
                ),
                client.get(
                    f"/things/{candidate_id}/images",
                    headers=self._auth_headers(None),
                ),
            )
            self._raise_for_status(thing_response, "load Thingiverse model page")
            self._raise_for_status(files_response, "load Thingiverse file list")
            self._raise_for_status(images_response, "load Thingiverse gallery")
            thing = thing_response.json()
            files = files_response.json()
            images = images_response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExternalServiceError(f"Thingiverse model inspection failed: {exc}") from exc

        if (
            not isinstance(thing, dict)
            or not isinstance(files, list)
            or not isinstance(images, list)
        ):
            raise ExternalServiceError("Thingiverse returned invalid model details")
        candidate = self._candidate_from_json(thing, files=files, images=images)
        if candidate is None:
            raise ExternalServiceError("Thingiverse model details were incomplete")

        sanitized_images: list[CatalogImage] = []
        for image_url in candidate.gallery_urls[: self.settings.max_gallery_images]:
            try:
                sanitized_images.append(await self._fetch_image(image_url))
            except (ExternalServiceError, PolicyViolationError):
                continue

        inspection = CandidatePageInspection(
            workflow_id=workflow_id,
            search_round_id=search_round_id,
            candidate=candidate,
            image_digests=[image.digest for image in sanitized_images],
        )
        return inspection, sanitized_images

    async def inspect_page(
        self,
        workflow_id: str,
        search_round_id: str,
        candidate_id: str,
    ) -> CandidatePageInspection:
        inspection, _ = await self.inspect_page_with_images(
            workflow_id,
            search_round_id,
            candidate_id,
        )
        return inspection

    async def download_file(
        self,
        candidate: ModelCandidate,
        file_id: str,
        destination: Path,
    ) -> Path:
        selected = next((item for item in candidate.files if item.id == file_id), None)
        if selected is None:
            raise PolicyViolationError("Selected file is not part of the inspected candidate")
        if selected.format.lower().lstrip(".") not in _SOURCE_FORMATS:
            raise PolicyViolationError("The selected source format is not supported")

        url = selected.download_url or f"/files/{selected.id}/download"
        parsed = urlparse(url)
        if parsed.hostname and parsed.hostname not in self._allowed_hosts:
            raise PolicyViolationError("Thingiverse download host is not allowlisted")

        client = await self._http()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        total = 0
        try:
            headers = self._auth_headers(url)
            headers["Referer"] = candidate.source_url
            async with client.stream(
                "GET",
                url,
                headers=headers,
            ) as response:
                self._raise_for_status(response, "download Thingiverse model file")
                final_host = response.url.host
                if final_host not in self._allowed_hosts:
                    raise PolicyViolationError("Thingiverse redirected to an untrusted host")
                declared_size = int(response.headers.get("content-length", "0") or 0)
                if declared_size > self.settings.max_download_bytes:
                    raise PolicyViolationError("Thingiverse file exceeds the download size limit")
                with temporary.open("wb") as stream:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.settings.max_download_bytes:
                            raise PolicyViolationError(
                                "Thingiverse file exceeds the download size limit"
                            )
                        stream.write(chunk)
            if total == 0:
                raise ExternalServiceError("Thingiverse returned an empty model file")
            temporary.replace(destination)
            return destination
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _candidate_from_json(
        self,
        item: object,
        *,
        files: list[object] | None = None,
        images: list[object] | None = None,
    ) -> ModelCandidate | None:
        if not isinstance(item, dict):
            return None
        thing_id = item.get("id")
        title = item.get("name") or item.get("title")
        if thing_id is None or not title:
            return None

        raw_files = files if files is not None else item.get("files", [])
        candidate_files = [
            candidate_file
            for raw in raw_files
            if (candidate_file := self._file_from_json(raw)) is not None
        ]

        raw_images = images if images is not None else item.get("images", [])
        gallery_urls = [
            image_url
            for raw in raw_images
            if (image_url := self._image_url(raw)) is not None
        ]

        raw_license = item.get("license")
        if isinstance(raw_license, dict):
            license_name = str(raw_license.get("name") or raw_license.get("id") or "unknown")
        else:
            license_name = str(raw_license or "unknown")
        normalized_license = license_name.strip().casefold()
        allows_derivatives = (
            False
            if normalized_license in _DERIVATIVE_BLOCKING_LICENSES
            or "no derivatives" in normalized_license
            else None
        )

        creator = item.get("creator")
        creator_name = (
            str(creator.get("name") or creator.get("username") or "unknown")
            if isinstance(creator, dict)
            else str(creator or "unknown")
        )
        tags = [
            str(tag.get("name") if isinstance(tag, dict) else tag)
            for tag in item.get("tags", [])
            if tag
        ]
        source_url = str(
            item.get("public_url")
            or item.get("url")
            or f"https://www.thingiverse.com/thing:{thing_id}"
        )
        popularity = {
            key: int(item.get(key, 0) or 0)
            for key in ("like_count", "collect_count", "make_count", "view_count")
        }
        return ModelCandidate(
            id=str(thing_id),
            title=str(title),
            introduction=sanitize_catalog_text(
                str(item.get("description") or item.get("details") or "")
            ),
            instructions=sanitize_catalog_text(str(item.get("instructions") or "")),
            tags=tags[:100],
            source_url=source_url,
            gallery_urls=gallery_urls[:20],
            files=candidate_files[:100],
            creator=creator_name,
            license=license_name,
            allows_derivatives=allows_derivatives,
            popularity=popularity,
        )

    @staticmethod
    def _file_from_json(item: object) -> CandidateFile | None:
        if not isinstance(item, dict) or item.get("id") is None:
            return None
        name = str(item.get("name") or item.get("filename") or "")
        extension = str(item.get("extension") or Path(name).suffix.lstrip(".")).lower()
        if extension not in _SOURCE_FORMATS:
            return None
        download_url = item.get("download_url") or item.get("url")
        return CandidateFile(
            id=str(item["id"]),
            name=name or f"{item['id']}.{extension}",
            format=extension,
            size_bytes=item.get("size"),
            download_url=str(download_url) if download_url else None,
        )

    @staticmethod
    def _image_url(item: object) -> str | None:
        if not isinstance(item, dict):
            return None
        sizes = item.get("sizes")
        if isinstance(sizes, list):
            valid_sizes = [
                size
                for size in sizes
                if isinstance(size, dict) and isinstance(size.get("url"), str)
            ]
            if valid_sizes:
                valid_sizes.sort(
                    key=lambda size: int(size.get("width", 0) or 0)
                    * int(size.get("height", 0) or 0),
                    reverse=True,
                )
                return str(valid_sizes[0]["url"])
        value = item.get("url") or item.get("display_url")
        return str(value) if value else None

    async def _fetch_image(self, url: str) -> CatalogImage:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in self._allowed_hosts:
            raise PolicyViolationError("Catalog image host is not allowlisted")
        client = await self._http()
        try:
            response = await client.get(url)
            self._raise_for_status(response, "load Thingiverse gallery image")
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"Thingiverse gallery request failed: {exc}") from exc
        if response.url.host not in self._allowed_hosts:
            raise PolicyViolationError("Catalog image redirected to an untrusted host")
        raw = response.content
        if len(raw) > self.settings.max_image_bytes:
            raise PolicyViolationError("Catalog image exceeds the byte limit")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if width * height > self.settings.max_image_pixels:
                    raise PolicyViolationError("Catalog image exceeds the pixel limit")
                image.load()
                sanitized = image.convert("RGB")
                output = io.BytesIO()
                sanitized.save(output, format="JPEG", quality=88, optimize=True)
        except (UnidentifiedImageError, OSError) as exc:
            raise PolicyViolationError("Catalog image is invalid") from exc
        data = output.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        cache_path = self.settings.candidate_cache_dir / "images" / f"{digest}.jpg"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not cache_path.exists():
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(cache_path)
        return CatalogImage(digest=digest, mime_type="image/jpeg", data=data)

    def _auth_headers(self, url: str | None) -> dict[str, str]:
        host = (
            urlparse(url).hostname
            if url
            else urlparse(self.settings.thingiverse_api_url).hostname
        )
        api_host = urlparse(self.settings.thingiverse_api_url).hostname
        if host == api_host:
            return {"Authorization": f"Bearer {self.settings.thingiverse_token}"}
        return {}

    @staticmethod
    def _raise_for_status(response: httpx.Response, operation: str) -> None:
        if response.status_code == 401:
            raise ConfigurationError("Thingiverse rejected the configured API token")
        if response.status_code == 429:
            raise ExternalServiceError("Thingiverse rate limit was exceeded")
        if response.is_error:
            raise ExternalServiceError(
                f"Could not {operation}: Thingiverse returned HTTP {response.status_code}"
            )
