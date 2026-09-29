from typing import Any, Optional, Type, TypeVar, Union
from urllib.parse import urljoin

from app.cfget import fetch_html
from bs4 import BeautifulSoup
from bs4.element import NavigableString, ResultSet, Tag

from app import MYDRAMALIST_WEBSITE
from app.handlers.parser import BaseSearch

T = TypeVar("T", bound="Catalog")

# Confirmed against MyDramaList's own embedded advanced-search filter config.
COUNTRY_CODES = {
    "japanese": 1,
    "chinese": 2,
    "korean": 3,
    "taiwanese": 4,
    "thai": 5,
    "other_asian": 6,
}

TYPE_CODES = {
    "series": 68,  # "Dramas" in MDL's own filter labels
    "movies": 77,
}

SORT_CODES = {
    "popular": "popular",
    "latest": "newest",
    "newest": "newest",
    "top": "top",
    "rated": "rated",
    "date": "date",
    "relevance": "relevance",
}


class Catalog(BaseSearch):
    """Catalog / advanced-search browse (country + type + sort filters)."""

    def __init__(self, soup: BeautifulSoup, query: str, code: int, ok: bool) -> None:
        super().__init__(soup, query, code, ok)

    @classmethod
    async def scrape_catalog(
        cls: Type[T], country: str, type_: str, sort: str, page: int = 1
    ) -> T:
        co = COUNTRY_CODES.get(country.lower())
        ty = TYPE_CODES.get(type_.lower())
        so = SORT_CODES.get(sort.lower())

        if co is None or ty is None or so is None:
            # invalid params -- return a non-ok instance, caller handles the error
            return cls(None, f"{country}/{type_}/{sort}", 400, False)

        if page < 1:
            page = 1

        url = (
            f"{MYDRAMALIST_WEBSITE.rstrip('/')}/search"
            f"?adv=titles&co={co}&ty={ty}&so={so}&page={page}"
        )

        ok = True
        code = 500
        soup = None
        try:
            code, text = fetch_html(url)
            soup = BeautifulSoup(text, "html.parser")
            ok = code == 200
        except Exception:
            ok = False

        return cls(soup, f"{country}/{type_}/{sort}", code, ok)

    def _get_container(self) -> Union[ResultSet, None]:
        if self.soup is None:
            return None
        results_container = self.soup.find("div", class_="col-lg-8 col-md-8")
        if results_container is None:
            return None
        return results_container.find_all("div", class_="box")

    def _res_get_ranking(self, result_container: Union[Tag, NavigableString]) -> Any:
        try:
            ranking_container = result_container.find(
                "div", class_="ranking pull-right"
            )
            if ranking_container is None:
                return None
            ranking = ranking_container.find("span")
            if ranking is None:
                return None
        except AttributeError:
            return None
        return ranking.text.strip()

    def _res_get_year_info(self, result_container: Union[Tag, NavigableString]):
        muted = result_container.find("span", class_="text-muted")
        if muted is None:
            return None, None, False
        _typeyear = muted.text
        try:
            t = _typeyear.split("-")[0].strip()
        except Exception:
            t = None
        try:
            _year_eps = _typeyear.split("-")[1]
        except Exception:
            _year_eps = ""
        year: Optional[int] = None
        try:
            year = int(_year_eps.split(",")[0].strip())
        except Exception:
            year = None
        try:
            series_ep: Union[str, bool] = _year_eps.split(",")[1].strip()
        except Exception:
            series_ep = False
        return t, year, series_ep

    def _get_catalog_results(self) -> None:
        results = self._get_container()
        if results is None:
            return
        _dramas = []
        for result in results:
            title_elem = result.find("h6", class_="text-primary title")
            if title_elem is None:
                continue
            r: Any = {}
            title_a = title_elem.find("a")
            if title_a is None:
                continue
            title = title_a.text.strip()
            url_slug = title_a.get("href")
            if url_slug is None:
                continue
            r["slug"] = str(url_slug).replace("/", "", 1)
            r["link"] = urljoin(MYDRAMALIST_WEBSITE, str(url_slug))
            r["title"] = title

            img = result.find("img", class_="img-responsive")
            _thumb = ""
            if img is not None:
                _raw = str(img.get("data-src", "")).split("/1280/")
                _thumb = _raw[1] if len(_raw) > 1 else _raw[0]
            r["thumb"] = _thumb

            if result.has_attr("id"):
                r["mdl_id"] = result["id"]

            r["ranking"] = self._res_get_ranking(result)
            r["type"], r["year"], r["series"] = self._res_get_year_info(result)

            score_elem = result.find("span", class_="score")
            if score_elem is not None and score_elem.text.strip():
                try:
                    r["rating"] = float(score_elem.text.strip())
                except Exception:
                    r["rating"] = None
            else:
                r["rating"] = None

            synopsis_ps = result.find_all("p")
            r["short_summary"] = ""
            for p in synopsis_ps:
                # skip the rating <p> (it only contains the score/rating spans)
                if p.find("span", class_="rating") is not None:
                    continue
                text = p.get_text().strip()
                if text:
                    r["short_summary"] = text
                    break

            _dramas.append(r)
        self.search_results["dramas"] = _dramas
        self.search_results["people"] = []
