"""Provider integration submodules (§6.3 OAuth, §6.4 inbound webhooks, §6.5 VCS)."""

from orchestratord.integrations.github_app import (
    GitHubAppOAuth,
    github_app_from_env,
)
from orchestratord.integrations.oauth import (
    LarkOAuth,
    OAuthError,
    SlackOAuth,
    oauth_from_env,
)

__all__ = [
    "GitHubAppOAuth",
    "LarkOAuth",
    "OAuthError",
    "SlackOAuth",
    "github_app_from_env",
    "oauth_from_env",
]
