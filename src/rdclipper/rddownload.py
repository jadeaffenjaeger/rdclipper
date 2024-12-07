import os
import threading
import time
from typing import List
from urllib.parse import urlparse

import click
import clipman
from dotenv import load_dotenv
from loguru import logger
from magnet_parser import magnet_decode
from rdapi import RD

load_dotenv()

if "RD_APITOKEN" not in os.environ:
    logger.error("RD_APITOKEN not found in environment. Quitting.")
    raise EnvironmentError("RD_APITOKEN not found")


class RDDownloader(threading.Thread):
    def __init__(
        self,
        output: click.File,
        stop_event: threading.Event,
        poll_interval: float = 0.5,
    ) -> None:
        super().__init__()
        self.poll_interval = poll_interval
        self.stop_event = stop_event
        self.last_clipboard: str = ""
        self.api: RD = RD()
        self.available_hosts: List[str] = self.api.hosts.domains().json()
        self.collected_links: List[str] = []
        self.added_torrents: List[str] = []
        self.output: click.File = output
        clipman.init()
        clipman.set("")

    def update_torrent_state(self):
        """
        Check if any queued torrents have finished downloading and add their unrestricted
        files to the output list if so.
        """

        def is_finished(torrent_id: str) -> bool:
            torrent_info = self.api.torrents.info(torrent_id).json()
            if torrent_info["status"] == "downloaded":
                logger.debug(
                    f"Torrent {torrent_id} has finished downloading. Parsing Links."
                )
                for link in torrent_info["links"]:
                    self.handle_hoster_link(link)
                return True
            return False

        self.added_torrents = [t for t in self.added_torrents if not is_finished(t)]

    def update(self):
        # Check if any torrents have finished since the last update
        self.update_torrent_state()

        # Write out recently added links
        if len(self.collected_links) >= 1:
            self.output.write("\n".join(self.collected_links) + "\n")
            self.collected_links = []

    def torrent_already_queued(self, info_hash: str) -> bool:
        """
        Check if a given info_hash is already in the list of torrents

        Args:
            info_hash: Torrent info_hash value to search for

        Returns:
            True if torrent already exists
            False otherwise
        """
        torrents_hashes = [t["hash"] for t in self.api.torrents.get().json()]
        return any([info_hash == t for t in torrents_hashes])

    def handle_torrent_link(self, uri: str):
        """
        Queue a magnet link for download

        Args:
            uri: magnet link to queue
        """
        magnet_hash = magnet_decode(uri).info_hash

        if self.torrent_already_queued(magnet_hash):
            logger.warning(f"Magnet link {uri} has been added in the past. Ignoring.")
            return

        torrent_id = self.api.torrents.add_magnet(uri).json()["id"]
        self.api.torrents.select_files(torrent_id, "all")
        logger.info(f"Added URI {uri} with torrent id {torrent_id}")
        self.added_torrents.append(torrent_id)

    def handle_hoster_link(self, uri: str):
        """
        Unrestrict an http(s) link for a supported hoster

        Args:
            uri: the link to unrestrict
        """
        dl = self.api.unrestrict.link(uri).json()
        if "download" in dl:
            self.collected_links.append(dl["download"])
            logger.info(f"Adding download link {dl["download"]} to collected links")
        else:
            logger.error(f"Could not add: {uri}")

    def parse_clipboard(self, clipboard_content: str):
        """
        Check if clipboard content contains a relevant link

        Args:
            clipboard_content: the clipboard content to parse
        """
        parsed_url = urlparse(clipboard_content)
        if parsed_url.scheme == "magnet":
            self.handle_torrent_link(clipboard_content)
        elif (
            parsed_url.scheme == "http" or parsed_url.scheme == "https"
        ) and parsed_url.netloc in self.available_hosts:
            self.handle_hoster_link(clipboard_content)

    def run(self):
        while not self.stop_event.is_set():
            try:
                current_clipboard = clipman.get()
                if current_clipboard != self.last_clipboard:
                    self.last_clipboard = current_clipboard
                    self.parse_clipboard(current_clipboard)
            except Exception as e:
                logger.error(e)
            time.sleep(self.poll_interval)
        # Write collected links to output file before shutting down
        self.update()


@click.command()
@click.option(
    "-o",
    "--output",
    default="rdclipper_urls.txt",
    type=click.File(mode="a+"),
    help="Output links file",
)
def main(output):
    stop_event = threading.Event()
    monitor = RDDownloader(output, stop_event)
    monitor.start()
    logger.debug("Clipboard monitor is running. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.debug("Stopping clipboard monitor...")
        stop_event.set()
        monitor.join()
        logger.debug("Clipboard monitor stopped.")


if __name__ == "__main__":
    main()
