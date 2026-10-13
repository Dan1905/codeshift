"""Built-in migration packs."""

from __future__ import annotations

from codeshift.rules.builtins.async_rules import BlockingCallInAsyncRule, SyncIOCandidateRule
from codeshift.rules.builtins.deprecated_api import (
    CollectionsAbcRule,
    UtcNowRule,
    logging_warn_rule,
)
from codeshift.rules.builtins.modernization import (
    BareExceptRule,
    OSErrorAliasRule,
    TargetVersionSyntaxRule,
)
from codeshift.rules.registry import Rule


def builtin_rules() -> list[Rule]:
    return [
        UtcNowRule(),
        CollectionsAbcRule(),
        logging_warn_rule(),
        OSErrorAliasRule(),
        BareExceptRule(),
        TargetVersionSyntaxRule(),
        BlockingCallInAsyncRule(),
        SyncIOCandidateRule(),
    ]
