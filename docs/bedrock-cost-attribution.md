# Attributing Bedrock spend on the bill itself

The telemetry pipeline attributes spend per user and team from what
Claude Code reports. That's the operational view. For the *billing*
view -- the one Finance reconciles and Cost Explorer can slice -- you
need the attribution to exist on the AWS bill, and that's what
**application inference profiles** are for.

## The pattern

1. Create one application inference profile per team (or product, or
   environment), wrapping the foundation model that team uses:

   ```bash
   aws bedrock create-inference-profile \
     --inference-profile-name team-platform-sonnet \
     --model-source copyFrom=arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-5-20250929-v1:0 \
     --tags key=team,value=platform key=cost_center,value=eng-123
   ```

2. Point each team's Claude Code at its profile ARN instead of the
   bare model ID (`ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` when
   using Bedrock):

   ```bash
   export CLAUDE_CODE_USE_BEDROCK=1
   export ANTHROPIC_MODEL='arn:aws:bedrock:us-east-1:<account>:application-inference-profile/<id>'
   ```

3. Activate the `team` / `cost_center` tags as cost allocation tags
   (Billing console -> Cost allocation tags; takes ~24h to appear).

Now Cost Explorer can group Bedrock spend by team with billing-grade
accuracy, `scripts/reconcile.py --tag-key team --tag-value platform`
can compare a single team's telemetry against its actual bill, and
IAM policies can scope which teams may invoke which profiles.

## Why do both?

| | Telemetry (this project) | Inference profiles + tags |
|---|---|---|
| Granularity | per user, per session, per model | per profile (team/product) |
| Latency | ~1 minute | ~24 hours |
| Covers | Claude Code usage | all invocations through the profile |
| Source of truth for | engineering behavior, cache efficiency, ROI | the actual bill |

Telemetry answers "who and how"; the bill answers "exactly how much."
The reconcile script keeps the two honest against each other -- if
estimated cost drifts from billed cost, either the pricing table is
stale or something else in the account is using Bedrock.
