"""Parser for kodeksy-kz.com - Kazakhstan legal codes and laws."""
import asyncio
import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag

from app.models.legislation import CategoryType, LawStatus

logger = logging.getLogger(__name__)

KODEKSY_BASE = "https://kodeksy-kz.com"

# Law status detection
STATUS_KEYWORDS = {
    "Утратил силу": LawStatus.REPEALED,
    "Утратило силу": LawStatus.REPEALED,
    "Отменен": LawStatus.REPEALED,
}


class KodeksyParser:
    """Parser for kodeksy-kz.com."""

    def __init__(self, lang: str = "ka"):
        self.lang = lang
        self.client = httpx.AsyncClient(
            verify=False,
            follow_redirects=True,
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
        )

    async def close(self):
        await self.client.aclose()

    async def fetch_page(self, url: str) -> str:
        resp = await self.client.get(url)
        resp.raise_for_status()
        return resp.text

    async def get_law_toc(self, slug: str) -> list[dict]:
        """Fetch the TOC page and extract article numbers and titles."""
        url = f"{KODEKSY_BASE}/{self.lang}/{slug}.htm"
        html = await self.fetch_page(url)
        soup = BeautifulSoup(html, "html.parser")

        toc_list = soup.find("ul", class_="no_dsk")
        if not toc_list:
            logger.warning(f"No TOC list found for {slug}")
            return []

        articles = []
        for li in toc_list.find_all("li"):
            a = li.find("a", class_="st-l")
            if not a:
                continue
            href = a.get("href", "")
            title = a.get_text(strip=True)
            # Extract article number from href (e.g., /ka/o_too_i_tdo/1.htm -> 1)
            m = re.search(r"/(\d+)\.htm$", href)
            if m:
                article_num = m.group(1)
                # Clean title (remove "Статья N." prefix if present)
                clean_title = re.sub(r"^Статья\s+[\d\-]+\.?\s*", "", title).strip()
                articles.append({
                    "number": article_num,
                    "title": clean_title or "",
                    "href": href,
                })
        return articles

    async def get_article_content(self, slug: str, article_num: str) -> Optional[dict]:
        """Fetch a single article page and extract its content."""
        url = f"{KODEKSY_BASE}/{self.lang}/{slug}/{article_num}.htm"
        html = await self.fetch_page(url)
        soup = BeautifulSoup(html, "html.parser")

        container = soup.find("div", class_="container")
        if not container:
            logger.warning(f"No container div for {slug}/{article_num}")
            return None

        # Extract article heading and content
        full_text = container.get_text("\n", strip=True)

        # Find the article heading (Статья X. Title)
        heading = ""
        content_parts = []
        for tag in container.find_all(["p", "center"]):
            text = tag.get_text(strip=True)
            if not text:
                continue
            # Статья heading
            m = re.match(r"^(Статья\s+[\d\-]+\.?\s*.*?)(?:\n|$)", text)
            if m:
                heading = m.group(1).strip()
            # Skip metadata lines (Ст. N Закон..., Внимание!, etc.)
            if re.match(r"^(Ст\.|Внимание|Версия|Примечание)", text):
                continue
            if heading and text not in heading:
                content_parts.append(text)

        if not heading:
            # Fallback: use first paragraph as heading
            heading = full_text[:100]

        content = "\n".join(content_parts) if content_parts else full_text

        return {
            "number": article_num,
            "title": heading,
            "content": content,
        }

    async def get_full_law(self, slug: str, max_concurrent: int = 10) -> Optional[dict]:
        """Fetch complete law: title + all articles (concurrently)."""
        url = f"{KODEKSY_BASE}/{self.lang}/{slug}.htm"
        html = await self.fetch_page(url)
        soup = BeautifulSoup(html, "html.parser")

        # Title from h1
        h1 = soup.find("h1")
        if not h1:
            logger.warning(f"No h1 found for {slug}")
            return None
        title = h1.get_text(strip=True)

        # Detect status
        status = LawStatus.ACTIVE
        page_text = soup.get_text()
        for keyword, law_status in STATUS_KEYWORDS.items():
            if keyword in page_text:
                status = law_status
                break

        # Get TOC
        toc = await self.get_law_toc(slug)
        if not toc:
            logger.warning(f"No articles found for {slug}")
            return None

        # Fetch all articles concurrently
        sem = asyncio.Semaphore(max_concurrent)

        async def fetch_article(art: dict):
            async with sem:
                return await self.get_article_content(slug, art["number"])

        article_results = await asyncio.gather(*[fetch_article(a) for a in toc])

        # Filter out failures
        articles = []
        for art in article_results:
            if art:
                articles.append(art)

        summary = None  # No useful summary from kodeksy

        return {
            "title": title,
            "number": None,
            "date_adopted": None,
            "status": status,
            "summary": summary,
            "full_text": None,
            "source_url": url,
            "articles": [
                {
                    "number": a["number"],
                    "title": a["title"],
                    "content": a["content"],
                    "sort_order": i,
                }
                for i, a in enumerate(articles)
            ],
        }

    async def extract_kodeksy_slugs(self) -> dict[str, str]:
        """Extract all available law slugs from kodeksy main page."""
        url = f"{KODEKSY_BASE}/{self.lang}/"
        html = await self.fetch_page(url)
        soup = BeautifulSoup(html, "html.parser")

        slugs = {}
        for a in soup.find_all("a", href=re.compile(rf"^/{self.lang}/.*\.htm$")):
            href = a["href"]
            title = a.get_text(strip=True)
            if not title or len(title) < 3:
                continue
            # Extract slug (e.g., /ka/zemelnyj_kodeks.htm -> zemelnyj_kodeks)
            m = re.search(r"/([\w_\-]+)\.htm$", href)
            if m:
                slug = m.group(1)
                if slug not in ("kodeksy", "zakony"):
                    slugs[slug] = title
        return slugs
