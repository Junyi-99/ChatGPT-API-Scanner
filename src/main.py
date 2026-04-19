"""
Scan GitHub for available OpenAI API Keys
"""

import argparse
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import rich
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumbase import Driver
from tqdm import tqdm

from configs import KEYWORDS, LANGUAGES, PATHS, REGEX_LIST
from manager import CookieManager, DatabaseManager, ProgressManager
from utils import check_key

FORMAT = "%(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT, datefmt="[%X]")
log = logging.getLogger("ChatGPT-API-Leakage")
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)


class APIKeyLeakageScanner:
    """
    Scan GitHub for available OpenAI API Keys
    """

    def __init__(self, db_file: str, keywords: list, languages: list):
        self.db_file = db_file
        self.driver: webdriver.Chrome | None = None
        self.cookies: CookieManager | None = None
        rich.print(f"📂 Opening database file {self.db_file}")

        self.dbmgr = DatabaseManager(self.db_file)

        self.keywords = keywords
        self.languages = languages
        self.candidate_urls = []
        for regex, too_many_results, _ in REGEX_LIST:
            # Add the paths to the search query
            for path in PATHS:
                self.candidate_urls.append(f"https://github.com/search?q=(/{regex.pattern}/)+AND+({path})&type=code&ref=advsearch")

            for language in self.languages:
                if too_many_results:  # if the regex is too many results, then we need to add AND condition
                    self.candidate_urls.append(f"https://github.com/search?q=(/{regex.pattern}/)+language:{language}&type=code&ref=advsearch")
                else:  # if the regex is not too many results, then we just need the regex
                    self.candidate_urls.append(f"https://github.com/search?q=(/{regex.pattern}/)&type=code&ref=advsearch")

    def login_to_github(self):
        """
        Login to GitHub
        """
        rich.print("🌍 Opening Chrome (SeleniumBase UC mode) ...")

        self.driver = Driver(
            uc=True,
            chromium_arg="--ignore-certificate-errors,--ignore-ssl-errors",
        )
        self.driver.implicitly_wait(3)

        self.cookies = CookieManager(self.driver)

        cookie_exists = os.path.exists("cookies.pkl")
        self.driver.uc_open_with_reconnect("https://github.com/login", reconnect_time=3)

        if not cookie_exists:
            rich.print("🤗 No cookies found, please login to GitHub first")
            input("Press Enter after you logged in: ")
            self.cookies.save()
        else:
            rich.print("🍪 Cookies found, loading cookies")
            self.cookies.load()

        self.cookies.verify_user_login()

    def _expand_all_code(self):
        """
        Expand all the code in the current page
        """
        elements = self.driver.find_elements(by=By.XPATH, value="//*[contains(text(), 'more match')]")
        for element in elements:
            element.click()

    def _find_urls_and_apis(self) -> tuple[list[str], list[str]]:
        """
        Find all the urls and apis in the current page
        """
        apis_found = []
        urls_need_expand = []

        codes = self.driver.find_elements(by=By.CLASS_NAME, value="code-list")  # type: ignore
        for element in codes:
            apis = []
            # Check all regex for each code block
            for regex, _, too_long in REGEX_LIST[2:]:
                if not too_long:
                    apis.extend(regex.findall(element.text))

            if len(apis) == 0:
                # Need to show full code. (because the api key is too long)
                # get the <a> tag
                a_tag = element.find_element(by=By.XPATH, value=".//a")
                urls_need_expand.append(a_tag.get_attribute("href"))
            apis_found.extend(apis)

        return apis_found, urls_need_expand

    def _process_url(self, url: str):
        """
        Process a search query url
        """
        if self.driver is None:
            raise ValueError("Driver is not initialized")

        self.driver.get(url)

        while True:  # Loop until all the pages are processed
            # If current webpage is reached the rate limit, then wait for 30 seconds
            if self.driver.find_elements(by=By.XPATH, value="//*[contains(text(), 'You have exceeded a secondary rate limit')]"):
                for _ in tqdm(range(30), desc="⏳ Rate limit reached, waiting ..."):
                    time.sleep(1)
                self.driver.refresh()
                continue

            self._expand_all_code()

            apis_found, urls_need_expand = self._find_urls_and_apis()
            rich.print(f"    🌕 There are {len(urls_need_expand)} urls waiting to be expanded")

            try:
                next_buttons = self.driver.find_elements(by=By.XPATH, value="//a[@aria-label='Next Page']")
                rich.print("🔍 Clicking next page")
                WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.XPATH, "//a[@aria-label='Next Page']")))
                next_buttons = self.driver.find_elements(by=By.XPATH, value="//a[@aria-label='Next Page']")
                next_buttons[0].click()
            except Exception:  # pylint: disable=broad-except
                rich.print("⚪️ No more pages")
                break

        # Handle the expand_urls
        for u in tqdm(urls_need_expand, desc="🔍 Expanding URLs ..."):
            if self.driver is None:
                raise ValueError("Driver is not initialized")

            with self.dbmgr as mgr:
                if mgr.get_url(u):
                    rich.print(f"    🔑 skipping url '{u[:10]}...{u[-10:]}'")
                    continue

            self.driver.get(u)
            time.sleep(3)  # TODO: find a better way to wait for the page to load # pylint: disable=fixme

            retry = 0
            while retry <= 3:
                matches = []
                for regex, _, _ in REGEX_LIST:
                    matches.extend(regex.findall(self.driver.page_source))
                matches = list(set(matches))

                if len(matches) == 0:
                    rich.print(f"    ⚪️ No matches found in the expanded page, retrying [{retry}/3]...")
                    retry += 1
                    time.sleep(3)
                    continue

                with self.dbmgr as mgr:
                    new_apis = [api for api in matches if not mgr.key_exists(api)]
                    new_apis = list(set(new_apis))
                apis_found.extend(new_apis)
                rich.print(f"    🔬 Found {len(matches)} matches in the expanded page, adding them to the list")
                for match in matches:
                    rich.print(f"        '{match[:10]}...{match[-10:]}'")

                with self.dbmgr as mgr:
                    mgr.insert_url(url)
                break

        self.check_api_keys_and_save(apis_found)

    def check_api_keys_and_save(self, keys: list[str]):
        """
        Check a list of API keys
        """
        with self.dbmgr as mgr:
            unique_keys = list(set(keys))
            unique_keys = [api for api in unique_keys if not mgr.key_exists(api)]

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(check_key, unique_keys))
            with self.dbmgr as mgr:
                for idx, result in enumerate(results):
                    mgr.insert(unique_keys[idx], result)

    def search(self, from_iter: int | None = None):
        """
        Search for API keys, and save the results to the database
        """
        progress = ProgressManager()
        total = len(self.candidate_urls)
        pbar = tqdm(
            enumerate(self.candidate_urls),
            total=total,
            desc="🔍 Searching ...",
        )
        if from_iter is None:
            from_iter = progress.load(total=total)

        for idx, url in enumerate(self.candidate_urls):
            if idx < from_iter:
                pbar.update()
                time.sleep(0.05)  # let tqdm print the bar
                log.debug("⚪️ Skip %s", url)
                continue
            self._process_url(url)
            progress.save(idx, total)
            log.debug("🔍 Finished %s", url)
            pbar.update()
        pbar.close()

    def deduplication(self):
        """
        Deduplicate the database
        """
        with self.dbmgr as mgr:
            mgr.deduplicate()

    def update_existed_keys(self):
        """
        Update previously checked API keys in the database with their current status
        """
        with self.dbmgr as mgr:
            rich.print("🔄 Updating existed keys")
            keys = mgr.all_keys()
            for key in tqdm(keys, desc="🔄 Updating existed keys ..."):
                result = check_key(key[0])
                mgr.delete(key[0])
                mgr.insert(key[0], result)

    def update_iq_keys(self):
        """
        Update insuffcient quota keys
        """
        with self.dbmgr as mgr:
            rich.print("🔄 Updating insuffcient quota keys")
            keys = mgr.all_iq_keys()
            for key in tqdm(keys, desc="🔄 Updating insuffcient quota keys ..."):
                result = check_key(key[0])
                mgr.delete(key[0])
                mgr.insert(key[0], result)

    def all_available_keys(self) -> list:
        """
        Get all available keys
        """
        with self.dbmgr as mgr:
            return mgr.all_keys()

    def __del__(self):
        if hasattr(self, "driver") and self.driver is not None:
            self.driver.quit()


def main(from_iter: int | None = None, check_existed_keys_only: bool = False, keywords: list | None = None, languages: list | None = None, check_insuffcient_quota: bool = False):
    """
    Main function to scan GitHub for available OpenAI API Keys
    """
    keywords = KEYWORDS.copy() if keywords is None else keywords
    languages = LANGUAGES.copy() if languages is None else languages

    leakage = APIKeyLeakageScanner("github.db", keywords, languages)

    if not check_existed_keys_only:
        leakage.login_to_github()
        leakage.search(from_iter=from_iter)

    if check_insuffcient_quota:
        leakage.update_iq_keys()

    leakage.update_existed_keys()
    leakage.deduplication()
    keys = leakage.all_available_keys()

    rich.print(f"🔑 [bold green]Available keys ({len(keys)}):[/bold green]")
    for key in keys:
        rich.print(f"[bold green]{key[0]}[/bold green]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-iter", type=int, default=None, help="Start from the specific iteration")
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug mode, otherwise INFO mode. Default is False (INFO mode)",
    )
    parser.add_argument(
        "-ceko",
        "--check-existed-keys-only",
        action="store_true",
        default=False,
        help="Only check existed keys",
    )
    parser.add_argument(
        "-ciq",
        "--check-insuffcient-quota",
        action="store_true",
        default=False,
        help="Check and update status of the insuffcient quota keys",
    )
    parser.add_argument(
        "-k",
        "--keywords",
        nargs="+",
        default=KEYWORDS,
        help="Keywords to search",
    )
    parser.add_argument(
        "-l",
        "--languages",
        nargs="+",
        default=LANGUAGES,
        help="Languages to search",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    main(
        from_iter=args.from_iter,
        check_existed_keys_only=args.check_existed_keys_only,
        keywords=args.keywords,
        languages=args.languages,
        check_insuffcient_quota=args.check_insuffcient_quota,
    )
