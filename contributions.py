# Copyright (c) 2020 Novo Nordisk Foundation Center for Biosustainability,
# Technical University of Denmark.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Collect code contributions to the DD-DeCaF organization on GitHub."""


import argparse
import asyncio
import logging
from collections import defaultdict
from getpass import getpass
from operator import itemgetter
from typing import Dict, List, DefaultDict, NamedTuple, Iterable

import aiometer
import httpx
import humanize
from pydantic import BaseModel, Field, HttpUrl, parse_obj_as


logger = logging.getLogger("contributions")


########################################################################################
# Organization Detail
########################################################################################


class OrganizationDetail(BaseModel):
    """"""

    id: int
    login: str
    repos_url: HttpUrl


async def get_organization_detail(
    client: httpx.AsyncClient, organization: str
) -> OrganizationDetail:
    """"""
    response = await client.get(f"/orgs/{organization}")
    response.raise_for_status()
    return OrganizationDetail.parse_raw(response.text)


########################################################################################
# Repository List
########################################################################################


class Repository(BaseModel):
    """"""

    id: int
    name: str
    url: HttpUrl


async def get_repositories(client: httpx.AsyncClient, url: str) -> List[Repository]:
    repos = []
    while True:
        response = await client.get(
            url, params={"type": "all", "sort": "full_name", "direction": "asc"}
        )
        response.raise_for_status()
        repos.extend(parse_obj_as(List[Repository], response.json()))
        if "next" not in response.links:
            break
        url = response.links["next"]["url"]
        logger.debug("Retrieving next page of repositories.\n%r", url)
    return repos


########################################################################################
# Repository Contributions
########################################################################################


class WeeklyContribution(BaseModel):
    """"""

    week_start: int = Field(..., alias="w")
    additions: int = Field(..., alias="a")
    deletions: int = Field(..., alias="d")
    commits: int = Field(..., alias="c")


class Author(BaseModel):
    """"""

    id: int
    login: str
    html_url: HttpUrl


class Contribution(BaseModel):
    """"""

    total: int
    weeks: List[WeeklyContribution]
    author: Author


class RepositoryArguments(NamedTuple):
    """"""

    client: httpx.AsyncClient
    slug: str


async def get_contributions_by_login(
    args: RepositoryArguments,
) -> Dict[str, List[WeeklyContribution]]:
    """"""
    logger.info("Retrieving %r contributions.", args.slug)
    url = f"/repos/{args.slug}/stats/contributors"
    exponent = 0
    while True:
        response = await args.client.get(url, timeout=None)
        # GitHub API returns 202 to mean that contribution statistics will be
        # calculated in the background.
        if response.status_code != 202:
            break
        time = 2 ** exponent
        logger.debug(
            "Backing off for %d s retrieving contributions to %r.", time, args.slug
        )
        await asyncio.sleep(time)
        exponent += 1
    response.raise_for_status()
    contributions: List[Contribution] = parse_obj_as(
        List[Contribution], response.json()
    )
    return {
        c.author.login: [w for w in c.weeks if w.commits > 0] for c in contributions
    }


########################################################################################
# Summarize
########################################################################################


async def summarize_contributions(
    organization: str,
    username: str,
    token: str,
    allow: Iterable = (),
    deny: Iterable = (),
) -> Dict[str, int]:
    """"""
    headers = {
        "Authorization": f"token {token}",
        "User-Agent": username,
        "Accept": "application/vnd.github.v3+json",
    }
    # The GitHub API may punish a burst of too many requests. We therefore limit our
    # requests to five per second.
    async with httpx.AsyncClient(
        base_url="https://api.github.com", headers=headers,
    ) as client:
        logger.info("Retrieving GitHub organization details.")
        org_detail = await get_organization_detail(client, organization)
        logger.info("Retrieving organization's repositories.")
        repos = await get_repositories(client, org_detail.repos_url)
        # Filter the identified repositories using the list of allowed and denied
        # entries.
        names = {r.name for r in repos}
        if allow:
            names.intersection_update(allow)
        if deny:
            names.difference_update(deny)
        logger.info("Retrieving repository contributions.")
        result: DefaultDict[str, int] = defaultdict(int)
        args = [RepositoryArguments(client, f"{organization}/{n}") for n in names]
        async with aiometer.amap(
            get_contributions_by_login, args, max_per_second=5
        ) as contributions:
            async for repo_contrib in contributions:
                for author, contribs in repo_contrib.items():
                    result[author] += sum(w.additions + w.deletions for w in contribs)
    return dict(result)


def main():
    """"""
    parser = argparse.ArgumentParser(
        description="Summarize code contributions to all repositories in a GitHub "
        "organization."
    )
    parser.add_argument(
        "organization",
        metavar="ORGANIZATION",
        help="The name of a GitHub organization.",
    )
    parser.add_argument(
        "username",
        metavar="USERNAME",
        help="The GitHub username who performs the requests.",
    )
    parser.add_argument(
        "--verbosity",
        help="The desired log level (default WARNING).",
        choices=("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"),
        default="WARNING",
    )
    parser.add_argument(
        "--allow",
        help="A file where each line contains the name of a repository to include.",
        type=argparse.FileType("r"),
    )
    parser.add_argument(
        "--deny",
        help="A file where each line contains the name of a repository to exclude.",
        type=argparse.FileType("r"),
    )
    args = parser.parse_args()
    logging.basicConfig(level=args.verbosity, format="[%(levelname)s] %(message)s")
    token = getpass("Token: ")
    if args.allow is not None:
        allow = {l.strip() for l in args.allow.readlines()}
    else:
        allow = ()
    if args.deny is not None:
        deny = {l.strip() for l in args.deny.readlines()}
    else:
        deny = ()
    summary = asyncio.run(
        summarize_contributions(args.organization, args.username, token, allow, deny)
    )
    for author, changes in sorted(summary.items(), key=itemgetter(1), reverse=True):
        print(author, humanize.intcomma(changes))


if __name__ == "__main__":
    main()
