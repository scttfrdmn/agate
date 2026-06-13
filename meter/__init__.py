"""Authoritative spend metering (design §7.2, §13.6).

The browser's cost figure is a non-authoritative estimate; the ENFORCED number is
recomputed server-side from Bedrock invocation logging × Price List rates and
written to the `spend` table, which the soft cap reads at credential refresh. This
package holds the pure log-record → spend-row translation (AWS-free, tested); the
S3-triggered Lambda and the CDK `audit.py` stack are the assembly around it.
"""

from meter.parse import (
    SpendRecord,
    parse_invocation_record,
    spend_key,
    spend_rollup_key,
)

__all__ = [
    "SpendRecord",
    "parse_invocation_record",
    "spend_key",
    "spend_rollup_key",
]
