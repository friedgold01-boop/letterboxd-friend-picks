
import random
import re
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
import streamlit as st
from bs4 import BeautifulSoup


BASE_URL = "https://letterboxd.com"
USER_AGENT = "FriendPicksPersonalPrototype/0.1 (+personal non-commercial prototype)"
REQUEST_DELAY_SECONDS = 1.5


@dataclass(frozen=True)
class Film:
    slug: str
    title: str
    url: str


@dataclass
class FriendPick:
    film: Film
    friend_ratings: Dict[str, float]

    @property
    def average_rating(self) -> float:
        return sum(self.friend_ratings.values()) / len(self.friend_ratings)

    @property
    def friend_count(self) -> int:
        return len(self.friend_ratings)

    @property
    def score(self) -> float:
        # Prioritise agreement first, then intensity.
        return self.friend_count * 10 + self.average_rating * 2


class LetterboxdScraper:
    def __init__(self, delay_seconds: float = REQUEST_DELAY_SECONDS):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.delay_seconds = delay_seconds
        self._last_request_at = 0.0
        self._robots = RobotFileParser()
        self._robots.set_url(urljoin(BASE_URL, "/robots.txt"))
        try:
            self._robots.read()
        except Exception:
            # If robots.txt cannot be read, be conservative but don't crash the UI.
            pass

    def _get(self, path_or_url: str) -> Optional[str]:
        url = path_or_url if path_or_url.startswith("http") else urljoin(BASE_URL, path_or_url)

        if hasattr(self._robots, "can_fetch") and not self._robots.can_fetch(USER_AGENT, url):
            st.warning(f"Skipped because robots.txt disallows this URL for this user agent: {url}")
            return None

        elapsed = time.time() - self._last_request_at
        if elapsed < self.delay_seconds:
            time.sleep(self.delay_seconds - elapsed)

        response = self.session.get(url, timeout=20)
        self._last_request_at = time.time()

        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    @staticmethod
    def _page_count(soup: BeautifulSoup) -> int:
        pages = [1]
        for link in soup.select(".paginate-pages a, .pagination a"):
            text = link.get_text(strip=True)
            if text.isdigit():
                pages.append(int(text))
        return max(pages)

    @staticmethod
    def _films_from_soup(soup: BeautifulSoup) -> List[Film]:
        films: Dict[str, Film] = {}

        # Common Letterboxd film grid markup.
        for item in soup.select("[data-film-slug]"):
            slug = item.get("data-film-slug")
            if not slug:
                continue

            title = None
            img = item.select_one("img[alt]")
            if img:
                title = img.get("alt")

            film_link = item.select_one('a[href*="/film/"]')
            url = urljoin(BASE_URL, film_link["href"]) if film_link and film_link.has_attr("href") else f"{BASE_URL}/film/{slug}/"

            films[slug] = Film(slug=slug, title=title or slug.replace("-", " ").title(), url=url)

        # Fallback for text lists / changed markup.
        for link in soup.select('a[href^="/film/"]'):
            href = link.get("href", "")
            match = re.match(r"^/film/([^/]+)/?$", href)
            if not match:
                continue
            slug = match.group(1)
            title = link.get_text(strip=True) or slug.replace("-", " ").title()
            films.setdefault(slug, Film(slug=slug, title=title, url=urljoin(BASE_URL, href)))

        return list(films.values())

    def films_from_paginated_path(self, path: str, max_pages: int) -> List[Film]:
        first_html = self._get(path)
        if not first_html:
            return []

        first_soup = BeautifulSoup(first_html, "html.parser")
        total_pages = min(self._page_count(first_soup), max_pages)

        results = self._films_from_soup(first_soup)

        for page in range(2, total_pages + 1):
            html = self._get(f"{path.rstrip('/')}/page/{page}/")
            if not html:
                break
            soup = BeautifulSoup(html, "html.parser")
            results.extend(self._films_from_soup(soup))

        # Deduplicate by slug, preserving order.
        unique: Dict[str, Film] = {}
        for film in results:
            unique.setdefault(film.slug, film)
        return list(unique.values())

    def user_watched_films(self, handle: str, max_pages: int) -> Set[str]:
        handle = clean_handle(handle)
        films = self.films_from_paginated_path(f"/{handle}/films/", max_pages=max_pages)
        return {film.slug for film in films}

    def user_high_rated_films(self, handle: str, ratings: Iterable[float], max_pages_per_rating: int) -> Dict[str, Tuple[Film, float]]:
        handle = clean_handle(handle)
        output: Dict[str, Tuple[Film, float]] = {}

        for rating in ratings:
            rating_path = rating_to_path_value(rating)
            path = f"/{handle}/films/rated/{rating_path}/"
            films = self.films_from_paginated_path(path, max_pages=max_pages_per_rating)

            for film in films:
                previous = output.get(film.slug)
                if previous is None or rating > previous[1]:
                    output[film.slug] = (film, rating)

        return output


def clean_handle(handle: str) -> str:
    handle = handle.strip()
    handle = handle.replace("https://letterboxd.com/", "")
    handle = handle.strip("/")
    return handle.split("/")[0]


def rating_to_path_value(rating: float) -> str:
    # Letterboxd rating filter URLs commonly use 4, 4.5, 5 etc.
    return str(int(rating)) if float(rating).is_integer() else str(rating)


def build_friend_picks(
    scraper: LetterboxdScraper,
    my_handle: str,
    friend_handles: List[str],
    min_rating: float,
    max_my_pages: int,
    max_friend_pages_per_rating: int,
) -> List[FriendPick]:
    ratings_to_check = [r for r in [5.0, 4.5, 4.0, 3.5] if r >= min_rating]

    watched = scraper.user_watched_films(my_handle, max_pages=max_my_pages)
    picks_by_slug: Dict[str, FriendPick] = {}

    progress = st.progress(0)
    for idx, friend in enumerate(friend_handles):
        friend = clean_handle(friend)
        if not friend:
            continue

        st.write(f"Checking **{friend}**…")
        high_rated = scraper.user_high_rated_films(
            friend,
            ratings=ratings_to_check,
            max_pages_per_rating=max_friend_pages_per_rating,
        )

        for slug, (film, rating) in high_rated.items():
            if slug in watched:
                continue

            if slug not in picks_by_slug:
                picks_by_slug[slug] = FriendPick(film=film, friend_ratings={})
            picks_by_slug[slug].friend_ratings[friend] = rating

        progress.progress((idx + 1) / max(len(friend_handles), 1))

    picks = list(picks_by_slug.values())
    picks.sort(key=lambda pick: (pick.score, pick.average_rating, pick.friend_count), reverse=True)
    return picks


st.set_page_config(page_title="Friend Picks for Letterboxd", page_icon="🎬", layout="wide")

st.title("🎬 Friend Picks for Letterboxd")
st.caption("Find films your friends rated highly that you haven’t watched yet.")

with st.expander("How this works", expanded=False):
    st.markdown(
        """
        This prototype reads public Letterboxd pages only. It does **not** log in,
        ask for passwords, or access private data.

        For a safer and more reliable long-term app, use Letterboxd exports or
        the official API if you get access.
        """
    )

my_handle = st.text_input("Your Letterboxd handle", placeholder="e.g. duncansmith")

friends_text = st.text_area(
    "Friend handles",
    placeholder="One per line, e.g.\nfriendone\nfriendtwo\nfriendthree",
    height=160,
)

col1, col2, col3 = st.columns(3)
with col1:
    min_rating = st.selectbox("Minimum friend rating", [5.0, 4.5, 4.0, 3.5], index=2)
with col2:
    max_my_pages = st.slider("Max pages of your watched films", 1, 30, 10)
with col3:
    max_friend_pages = st.slider("Max pages per friend rating", 1, 20, 5)

randomize = st.checkbox("Add a bit of randomness to top results", value=True)

if st.button("Find friend picks", type="primary"):
    friend_handles = [clean_handle(line) for line in friends_text.splitlines() if clean_handle(line)]

    if not my_handle.strip():
        st.error("Enter your Letterboxd handle.")
    elif not friend_handles:
        st.error("Enter at least one friend handle.")
    else:
        scraper = LetterboxdScraper()

        try:
            picks = build_friend_picks(
                scraper=scraper,
                my_handle=my_handle,
                friend_handles=friend_handles,
                min_rating=min_rating,
                max_my_pages=max_my_pages,
                max_friend_pages_per_rating=max_friend_pages,
            )
        except requests.HTTPError as exc:
            st.error(f"Letterboxd request failed: {exc}")
            st.stop()
        except requests.RequestException as exc:
            st.error(f"Network error: {exc}")
            st.stop()

        if not picks:
            st.warning("No picks found. Try more friends, more pages, or a lower rating threshold.")
            st.stop()

        st.success(f"Found {len(picks)} unseen friend-approved films.")

        top_pool = picks[: min(25, len(picks))]
        chosen = random.choice(top_pool) if randomize else top_pool[0]

        st.subheader("Tonight’s pick")
        st.markdown(f"## [{chosen.film.title}]({chosen.film.url})")
        st.write(
            f"Recommended by **{chosen.friend_count}** friend(s), "
            f"average friend rating **{chosen.average_rating:.2f}★**."
        )

        with st.expander("Who rated it highly?", expanded=True):
            for friend, rating in sorted(chosen.friend_ratings.items(), key=lambda item: item[1], reverse=True):
                st.write(f"**{friend}** — {rating}★")

        st.divider()
        st.subheader("Top candidates")

        for pick in picks[:50]:
            friends = ", ".join(
                f"{friend} {rating}★"
                for friend, rating in sorted(pick.friend_ratings.items(), key=lambda item: item[1], reverse=True)
            )
            st.markdown(
                f"**[{pick.film.title}]({pick.film.url})**  \n"
                f"{pick.friend_count} friend(s), avg {pick.average_rating:.2f}★ — {friends}"
            )
