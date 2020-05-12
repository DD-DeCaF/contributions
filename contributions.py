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
    """
    Return a detailed description of a GitHub organization.

    Args:
        client (httpx.AsyncClient): An asynchronous httpx client instance that has the
            correct headers for communicating with the GitHub API set already.
        organization (str): The name of the GitHub organization.

    Returns:
        OrganizationDetail: Specific fields of the extensive JSON response as described
            by the model definition.

    """
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
    """
    Return a list of GitHub repository descriptions.

    Args:
        client (httpx.AsyncClient): An asynchronous httpx client instance that has the
            correct headers for communicating with the GitHub API set already.
        url (str): The starting from which to fetch the repository list. Typically given
            by `OrganizationDetail.repos_url`.

    Returns:
        list: A collection of repository descriptions as described by the `Repository`
            model.

    """
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


async def get_contributions_by_author(
    args: RepositoryArguments,
) -> Dict[str, List[WeeklyContribution]]:
    """
    Retrieve code contributions to a specific repository as weekly reports by author.

    Args:
        args (RepositoryArguments): A `namedtuple` that includes all necessary
            arguments.

    Returns:
        dict: A map from authors (given by their GitHub usernames) to their code
            contributions as a list of weekly commits, additions, and deletions.

    """
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
    """
    Summarize all code contributions over all of an organization's repositories.

    Args:
        organization (str): The name of the GitHub organization.
        username (str): The name of the GitHub username making the requests. This is
            used for identification in the 'User-Agent' header.
        token (str): A personal GitHub token for authenticating with the GitHub API.
        allow: An iterable of repository names to consider exclusively from the
            organization.
        deny: An iterable of repository names to exclude from the list of repositories
            of a GitHub organization.

    Returns:
        dict: A map from author GitHub usernames to the number of code changes (added
            and deleted lines of code) they have contributed across all of an
            organization's repositories.

    """
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
            get_contributions_by_author, args, max_per_second=5
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
