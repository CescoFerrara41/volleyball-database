"""
Minimal client for the FIVB VIS web service.

This wraps the public, documented FIVB VIS Web Service
(https://www.fivb.org/VisSDK/VisWebService/) rather than scraping
volleyballworld.com's rendered frontend. Verified live on 2026-08-11
against https://www.fivb.org/Vis2009/XmlRequest.asmx.

The service takes a single 'Request' parameter: an XML <Request> element
naming a request Type, optionally with a Fields attribute (which
properties to return) and a nested <Filter> element (how to narrow
the results). It can return XML or, for a growing subset of requests,
JSON — we stick to XML here since that's what's confirmed working for
GetVolleyTournamentList / GetVolleyMatchList / GetVolleyMatch.
"""

import xml.etree.ElementTree as ET
from typing import Optional

import requests

VIS_ENDPOINT = "https://www.fivb.org/Vis2009/XmlRequest.asmx"

# Being a good citizen of a free public API: identify ourselves and
# don't hammer it. FIVB's docs mention an optional application
# identifier (contact vis.sdk@fivb.org) for higher-visibility usage;
# not required for basic read access but worth pursuing if this
# project grows.
HEADERS = {"User-Agent": "vnl-stats-portfolio-project/0.1 (personal project)"}


class VisClientError(RuntimeError):
    """Raised when the VIS service returns an error element."""


def _send(xml_request: str) -> ET.Element:
    """POST a raw <Request>...</Request> XML string and return the parsed root element."""
    resp = requests.post(
        VIS_ENDPOINT,
        data={"Request": xml_request},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    # Error responses come back as e.g. <BadRequestSyntax id="4"> or
    # <BadParameter id="1002">Type</BadParameter> instead of the
    # expected element name.
    if root.tag in ("BadRequestSyntax", "BadParameter", "Error"):
        raise VisClientError(f"VIS request failed ({root.tag}): {ET.tostring(root, encoding='unicode')}")

    return root


def get_tournament_list(fields: str = "No Code Name Season") -> list[dict]:
    """
    Fetch the full tournament list (no server-side filter — the service
    doesn't support a partial/contains match on Code, so we filter
    client-side, e.g. for 'VNL' in Code). This is one request for
    ~1500+ tournaments across FIVB's history, so it's cheap to just
    pull everything and filter in Python.
    """
    xml_request = f"<Request Type='GetVolleyTournamentList' Fields='{fields}'/>"
    root = _send(xml_request)
    return [el.attrib for el in root]


def get_match_list(tournament_no: int, fields: str) -> list[dict]:
    """Fetch matches for a single tournament by its VIS tournament number."""
    xml_request = (
        f"<Request Type='GetVolleyMatchList' Fields='{fields}'>"
        f"<Filter NoTournament='{tournament_no}'/>"
        f"</Request>"
    )
    root = _send(xml_request)
    return [el.attrib for el in root]


def get_match(match_no: int, fields: Optional[str] = None) -> dict:
    """Fetch full detail for a single match by its VIS match number."""
    fields_attr = f" Fields='{fields}'" if fields else ""
    xml_request = f"<Request Type='GetVolleyMatch' No='{match_no}'{fields_attr}/>"
    root = _send(xml_request)
    return root.attrib
