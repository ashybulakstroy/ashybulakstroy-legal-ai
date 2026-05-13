"""
Парсер нормативных правовых актов Республики Казахстан с сайта adilet.zan.kz.
"""
import logging
import re
from datetime import date, datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag

from app.models.legislation import Category, CategoryType, Law, LawStatus, Article

logger = logging.getLogger(__name__)

# Map adilet form types to our CategoryType
FORM_TYPE_MAP = {
    "Конституция": CategoryType.CONSTITUTION,
    "Кодекс": CategoryType.CODEX,
    "Закон": CategoryType.LAW,
}

ADILET_BASE = "https://adilet.zan.kz"


class AdiletParser:
    """Parser for adilet.zan.kz - the official Kazakhstan legal information system."""

    def __init__(self):
        self.client = httpx.Client(
            verify=False,
            follow_redirects=True,
            timeout=60,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
        )

    def close(self):
        self.client.close()

    def fetch_page(self, url: str) -> str:
        resp = self.client.get(url)
        resp.encoding = "utf-8"
        resp.raise_for_status()
        return resp.text

    def parse_document_list(self, form_type: str, page: int = 1) -> list[dict]:
        """
        Parse search results page for a given form type.
        form_type: 'ff=2' for codes, 'ff=3' for laws, etc.
        """
        url = f"{ADILET_BASE}/rus/search/docs/{form_type}&page={page}"
        html = self.fetch_page(url)
        soup = BeautifulSoup(html, "html.parser")

        docs = []
        # Each result is typically in a div with class containing "search-result" or similar
        # Look for links to /rus/docs/
        for link in soup.find_all("a", href=re.compile(r"^/rus/docs/")):
            href = link.get("href", "")
            if href in ("/rus/docs/rss",):
                continue
            title = link.get_text(strip=True)
            if not title or len(title) < 3:
                continue
            docs.append({
                "url": f"{ADILET_BASE}{href}",
                "doc_id": href.replace("/rus/docs/", ""),
                "title": title,
            })

        # Remove duplicates (same doc_id)
        seen = set()
        unique = []
        for d in docs:
            if d["doc_id"] not in seen:
                seen.add(d["doc_id"])
                unique.append(d)
        return unique

    def parse_document_page(self, url: str) -> Optional[dict]:
        """Parse a single document page and extract all metadata and content."""
        html = self.fetch_page(url)
        soup = BeautifulSoup(html, "html.parser")

        # Extract metadata from container_alpha
        alpha_div = soup.find("div", class_="container_alpha")
        if not alpha_div:
            logger.warning(f"No container_alpha found for {url}")
            return None

        # Title from h1
        h1 = alpha_div.find("h1")
        if not h1:
            logger.warning(f"No h1 found for {url}")
            return None
        title = h1.get_text(strip=True)

        # Meta description (number, date, status)
        meta_p = alpha_div.find("p")
        meta_text = meta_p.get_text(strip=True) if meta_p else ""

        # Determine document type from meta text or URL
        doc_type = self._detect_type(meta_text, url)

        # Parse number and dates from meta
        doc_number = self._parse_number(meta_text)
        date_adopted = self._parse_date_adopted(meta_text, doc_type)

        # Determine status
        status = self._parse_status(meta_text, soup)

        # Extract text content
        doc_div = soup.find("div", class_="module_npaView")
        if not doc_div:
            logger.warning(f"No module_npaView found for {url}")
            return None

        full_text = doc_div.get_text("\n", strip=True)
        summary = self._generate_summary(full_text)

        # Extract articles
        articles = self._extract_articles(doc_div)

        return {
            "title": title,
            "number": doc_number,
            "date_adopted": date_adopted,
            "status": status,
            "summary": summary,
            "full_text": full_text,
            "doc_type": doc_type,
            "meta_text": meta_text,
            "source_url": url,
            "articles": articles,
        }

    def _detect_type(self, meta_text: str, url: str) -> CategoryType:
        """Detect document category type from meta text and URL."""
        if "Конституция" in meta_text or "конституция" in meta_text.lower():
            return CategoryType.CONSTITUTION
        if "Кодекс" in meta_text:
            return CategoryType.CODEX
        if "Закон" in meta_text:
            return CategoryType.LAW
        # Fallback: check URL
        if "/K95" in url:
            return CategoryType.CONSTITUTION
        if url.startswith(f"{ADILET_BASE}/rus/docs/K"):
            return CategoryType.CODEX
        if url.startswith(f"{ADILET_BASE}/rus/docs/Z"):
            return CategoryType.LAW
        return CategoryType.OTHER

    def _parse_number(self, meta_text: str) -> Optional[str]:
        """Extract document number from meta text."""
        m = re.search(r"№\s*([\d\-\w]+)", meta_text)
        return m.group(1).strip() if m else None

    def _parse_date_adopted(self, meta_text: str, doc_type: CategoryType) -> Optional[date]:
        """Extract adoption date from meta text."""
        months_ru = {
            "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
            "мая": 5, "июня": 6, "июля": 7, "августа": 8,
            "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
        }
        if doc_type == CategoryType.CONSTITUTION:
            m = re.search(r"(\d+)\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})", meta_text)
        else:
            m = re.search(r"от\s+(\d+)\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})", meta_text)
        if m:
            day = int(m.group(1))
            month = months_ru.get(m.group(2), 1)
            year = int(m.group(3))
            try:
                return date(year, month, day)
            except ValueError:
                return None
        return None

    def _parse_status(self, meta_text: str, soup: BeautifulSoup) -> LawStatus:
        """Determine document status."""
        # Check for status indicators in the alpha slogan div
        slogan = soup.find("div", class_=re.compile(r"slogan"))
        if slogan:
            slogan_text = slogan.get_text(strip=True)
            if "Утративший силу" in slogan_text or "Утратил силу" in slogan_text:
                return LawStatus.REPEALED
            if "Обновленный" in slogan_text:
                return LawStatus.AMENDED

        # Check meta text
        if "Утратил силу" in meta_text or "утратил силу" in meta_text.lower():
            return LawStatus.REPEALED
        if "Обновленный" in meta_text or "Измененный" in meta_text:
            return LawStatus.AMENDED

        return LawStatus.ACTIVE

    def _generate_summary(self, full_text: str) -> Optional[str]:
        """Generate a summary from the beginning of the text."""
        # Take first ~500 chars as summary, up to a sentence boundary
        text = full_text.strip()
        if not text:
            return None
        # First paragraph or first 500 chars
        paragraphs = text.split("\n")
        summary = ""
        for p in paragraphs:
            p = p.strip()
            if p:
                summary += p + " "
            if len(summary) > 300:
                break
        return summary.strip()[:500] if summary else text[:500]

    def _extract_articles(self, doc_div: Tag) -> list[dict]:
        """Extract articles from the document div."""
        articles = []
        current_article = None
        current_content: list[str] = []
        sort_order = 0

        # Find all <p> tags and relevant headings recursively
        for tag in doc_div.find_all(["p", "h2", "h3", "h4"]):
            text = tag.get_text(strip=True)
            if not text:
                continue

            # Check if this is an article heading (Статья X. or Статья X)
            article_match = re.match(
                r"Статья\s+([\d\-]+)\.?\s*(.*)", text
            )

            if article_match:
                # Save previous article
                if current_article is not None:
                    current_article["content"] = "\n".join(current_content)
                    if current_article["content"].strip():
                        articles.append(current_article)
                    current_content = []

                current_article = {
                    "number": article_match.group(1),
                    "title": article_match.group(2).strip(),
                    "content": "",
                    "sort_order": sort_order,
                }
                sort_order += 1
            elif current_article is not None:
                current_content.append(text)

        # Save last article
        if current_article is not None:
            current_article["content"] = "\n".join(current_content)
            if current_article["content"].strip():
                articles.append(current_article)

        return articles

    def get_popular_codes(self) -> list[dict]:
        """Get the list of main codes from adilet."""
        # The main codes are available from the search with ff=2 filter
        return self.parse_document_list("ff=2")

    def get_laws(self, page: int = 1) -> list[dict]:
        """Get list of laws."""
        return self.parse_document_list("ff=3", page=page)

    def scrape_categories_and_laws(self) -> dict[str, list[dict]]:
        """
        Scrape all main documents organized by type.
        Returns dict with type -> [doc_data] mapping.
        """
        result = {}

        # Get codes
        logger.info("Fetching codes...")
        codes = self.get_popular_codes()
        result["codes"] = codes
        logger.info(f"Found {len(codes)} codes")

        # Get first page of laws
        logger.info("Fetching laws (page 1)...")
        laws = self.get_laws(page=1)
        result["laws"] = laws
        logger.info(f"Found {len(laws)} laws on page 1")

        return result
