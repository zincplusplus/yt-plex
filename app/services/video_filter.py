import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def matches_filter(text: str, pattern: str) -> bool:
    """Check if text matches a regex pattern."""
    if not pattern:
        return True
    if not text:
        return False
    try:
        return bool(re.search(pattern, text, re.IGNORECASE))
    except re.error as e:
        logger.error(f"Invalid regex pattern '{pattern}': {e}")
        return True  # On error, don't filter out


def check_video_filters(
    title: str,
    description: str,
    title_filter: Optional[str] = None,
    description_filter: Optional[str] = None,
    description_exclude: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Check if a video passes all filters.

    Returns:
        tuple: (passes, reason) - passes is True if video should be downloaded
    """

    # Check title filter (must match if specified)
    if title_filter:
        if not matches_filter(title, title_filter):
            return False, f"Title doesn't match filter: {title_filter}"

    # Check description filter (must match if specified)
    if description_filter:
        if not matches_filter(description or '', description_filter):
            return False, f"Description doesn't match filter: {description_filter}"

    # Check description exclude (must NOT match if specified)
    if description_exclude:
        if matches_filter(description or '', description_exclude):
            return False, f"Description matches exclude filter: {description_exclude}"

    return True, "Passed all filters"
