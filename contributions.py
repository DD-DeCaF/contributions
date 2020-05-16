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
import json
import logging
import sys
from collections import defaultdict
from getpass import getpass
from operator import itemgetter
from pathlib import Path
from time import sleep
from typing import DefaultDict, Dict, Iterable, List, Set, Tuple, Optional

import humanize
from gql import Client, gql
from graphql.language.ast import Document
from gql.transport.requests import RequestsHTTPTransport
from pydantic import BaseModel, Field, HttpUrl


formatting = logging.Formatter(fmt="[%(asctime)s] [%(levelname)s] %(message)s")
terminal = logging.StreamHandler()
terminal.setLevel(logging.INFO)
terminal.setFormatter(formatting)
log_file = logging.FileHandler("contributions.log", "w")
log_file.setLevel(logging.DEBUG)
log_file.setFormatter(formatting)
logger = logging.getLogger("contributions")
logger.addHandler(terminal)
logger.addHandler(log_file)


########################################################################################
# Utilities
########################################################################################


def execute(client: Client, query: Document, **kwargs) -> dict:
    response = client.execute(query, **kwargs)
    logger.debug("Response:\n%s", json.dumps(response, indent=2))
    return response


########################################################################################
# Repository List
########################################################################################


class PageInfo(BaseModel):

    has_next: bool = Field(..., alias="hasNextPage")
    end_cursor: str = Field(..., alias="endCursor")


class Repository(BaseModel):

    id: str
    name: str
    url: HttpUrl


class Repositories(BaseModel):

    nodes: List[Repository]
    page_info: PageInfo = Field(..., alias="pageInfo")
    total: int = Field(..., alias="totalCount")


class Organization(BaseModel):

    id: str
    repositories: Repositories


class OrgResponse(BaseModel):

    organization: Organization


def get_repositories(client: Client, organization: str) -> List[Repository]:
    """
    Return a list of all repository descriptions in an organization.

    Args:
        client (gql.Client): A GraphQL client configured with the appropriate HTTP
            headers.
        organization (str): The name of the GitHub organization.

    Returns:
        list: A collection of descriptions as defined by the `Repository` model.

    """
    query = gql(
        """
    query getOrganizationRepositories($org: String!, $cursor: String) {
      organization(login: $org) {
        id
        repositories(first: 50, after: $cursor) {
          totalCount
          nodes {
            id
            name
            url
          }
          pageInfo {
            hasNextPage
            endCursor
          }
        }
      }
      rateLimit {
        limit
        cost
        remaining
        resetAt
        nodeCount
      }
    }
    """
    )
    data = OrgResponse.parse_obj(
        execute(client, query, variable_values={"org": organization})
    )
    repos = data.organization.repositories
    result = repos.nodes
    while repos.page_info.has_next:
        logger.debug(
            "Retrieving next page of repositories from cursor %r.",
            repos.page_info.end_cursor,
        )
        data = OrgResponse.parse_obj(
            execute(
                client,
                query,
                variable_values={
                    "org": organization,
                    "cursor": repos.page_info.end_cursor,
                },
            )
        )
        repos = data.organization.repositories
        result.extend(repos.nodes)
    assert len(result) == repos.total
    return result


########################################################################################
# Repository Contributions
########################################################################################


class User(BaseModel):

    login: str


class Author(BaseModel):

    name: str
    email: str
    user: Optional[User]


class Commit(BaseModel):

    sha: str = Field(..., alias="oid")
    additions: int
    deletions: int
    author: Author


class History(BaseModel):

    nodes: List[Commit]
    page_info: PageInfo = Field(..., alias="pageInfo")
    total: int = Field(..., alias="totalCount")


class Target(BaseModel):

    history: History


class Branch(BaseModel):

    name: str
    target: Target


class BranchRepo(BaseModel):

    default_branch: Branch = Field(..., alias="defaultBranchRef")


class RepoResponse(BaseModel):

    repository: BranchRepo


def get_commits(client: Client, organization: str, name: str) -> List[Commit]:
    """
    Return the entire commit history of the default branch on a repository.

    Args:
        client (gql.Client): A GraphQL client configured with the appropriate HTTP
            headers.
        organization (str): The name of the GitHub organization.
        name (str): The name of the GitHub repository.

    Returns:
        list: A collection of commits representing the complete history of the default
            branch.

    """
    query = gql(
        """
    query getCommitInfo($org: String!, $name: String!, $cursor: String) {
      repository(name: $name, owner: $org) {
        defaultBranchRef {
          name
          target {
            ... on Commit {
              history(first: 100, after: $cursor) {
                nodes {
                  additions
                  deletions
                  author {
                    email
                    name
                    user {
                      login
                    }
                  }
                  oid
                }
                totalCount
                pageInfo {
                  endCursor
                  hasNextPage
                }
              }
            }
          }
        }
      }
      rateLimit {
        limit
        cost
        remaining
        resetAt
        nodeCount
      }
    }
    """
    )
    logger.info("Retrieving commits from %s's default branch.", name)
    for number in range(1, 8):
        data = execute( client, query, variable_values={"org": organization, "name": name})
        if data.get("repository", {}).get("defaultBranchRef") is None:
            duration = 2 ** number
            logger.warning("Backing off for %d s.", duration)
            sleep(duration)
            continue
        else:
            data = RepoResponse.parse_obj(data)
            break
    if number == 8:
        raise RuntimeError("Failed to get a proper response.")
    history = data.repository.default_branch.target.history
    commits = history.nodes
    while history.page_info.has_next:
        logger.debug(
            "Retrieving next page of commits from cursor %r.",
            history.page_info.end_cursor,
        )
        for number in range(1, 8):
            data = execute(client, query,
                           variable_values={"org": organization, "name": name,
                                            "cursor": history.page_info.end_cursor,
                                            })
            if data.get("repository", {}).get("defaultBranchRef") is None:
                duration = 2 ** number
                logger.warning("Backing off for %d s.", duration)
                sleep(duration)
                continue
            else:
                data = RepoResponse.parse_obj(data)
                break
        if number == 8:
            raise RuntimeError("Failed to get a proper response.")
        history = data.repository.default_branch.target.history
        commits.extend(history.nodes)
    assert len(commits) == history.total
    return commits


########################################################################################
# Summarize
########################################################################################


def summarize_contributions(
    organization: str,
    username: str,
    token: str,
    allow: Iterable = (),
    deny: Iterable = (),
) -> Tuple[Dict[str, int], Dict[str, Dict[str, Set[str]]]]:
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
        dict: A map from GitHub author emails to the number of code changes (added
            and deleted lines of code) they have contributed across all of an
            organization's repositories.

    """
    github_transport = RequestsHTTPTransport(
        url="https://api.github.com/graphql",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": username,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        },
        verify=True,
        use_json=True,
    )
    with Client(transport=github_transport) as client:
        logger.info("Retrieving %s's repositories.", organization)
        repos = get_repositories(client, organization)
        # Filter the identified repositories using the list of allowed and denied
        # entries.
        names = {r.name for r in repos}
        if allow:
            names.intersection_update(allow)
        if deny:
            names.difference_update(deny)

        result: DefaultDict[str, int] = defaultdict(int)
        authors = {}
        for commits in (get_commits(client, organization, n) for n in sorted(names)):
            for commit in commits:
                email = commit.author.email
                result[email] += commit.additions + commit.deletions
                auth = authors.setdefault(email, {})
                auth.setdefault("names", set()).add(commit.author.name)
                if (user := commit.author.user) is not None:
                    auth.setdefault("login", set()).add(user.login)
    return dict(result), authors


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
    logger.setLevel(args.verbosity)
    token = sys.stdin.read().rstrip("\r\n")
    if not token:
        token = getpass("Token: ")
    if args.allow is not None:
        allow = {l.strip() for l in args.allow.readlines()}
    else:
        allow = ()
    if args.deny is not None:
        deny = {l.strip() for l in args.deny.readlines()}
    else:
        deny = ()
    summary, authors = summarize_contributions(
        args.organization, args.username, token, allow, deny
    )
    for user, changes in sorted(summary.items(), key=itemgetter(1), reverse=True):
        print(user, humanize.intcomma(changes))
    with Path("authors.json").open("w") as handle:
        json.dump(authors, handle, default=list)


if __name__ == "__main__":
    main()
