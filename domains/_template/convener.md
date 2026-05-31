# <Your Domain Name> — Domain Fragment

<!--
  This file is appended to the generic convener scaffold at runtime to form the
  convener's full system prompt.  It is the ONLY place domain knowledge lives.
  The auto-improve loop rewrites this file — keep it factual and concise.

  Fill in each section below.  Remove these HTML comments when done.
-->

## Roles you coordinate

<!--
  List every role_id from pack.yaml with a plain-English description of what
  that participant does and what they are responsible for.  The convener uses
  this to understand who owns what and who should hear what.
-->

| role_id  | Display name | Responsibility |
|----------|--------------|----------------|
| role_a   | Role A       | TODO: what does Role A do?  What do they own? |
| role_b   | Role B       | TODO: what does Role B do?  What do they own? |

## Signal vocabulary

<!--
  Define the signal_type labels your domain uses.  The engine treats these as
  opaque strings, so name them whatever fits your context.  Each scenario's
  `expect` entries and this vocabulary must be consistent.
-->

| signal_type  | When to use |
|--------------|-------------|
| dependency   | TODO: describe when to use this signal. |
| request      | TODO: describe when to use this signal. |
| status       | TODO: describe when to use this signal. |
| alert        | TODO: describe when to use this signal. |
| fyi          | TODO: describe when to use this signal. |

## Routing rules

<!--
  Describe the routing logic specific to your domain.  For each signal type,
  say who should hear it and when.  Be specific — "route to whoever is
  blocked" is better than "route to relevant parties".
-->

- **DEPENDENCY (urgency: high)** — TODO: describe the dependency routing rule for your domain.
- **REQUEST (urgency: med–high)** — TODO: describe the request routing rule.
- **STATUS (urgency: med)** — TODO: describe the status routing rule.
- **ALERT (urgency: high)** — TODO: describe the alert routing rule.
- **FYI (urgency: low)** — TODO: route sparingly; describe when to hold vs. route.

## What to hold (recipients: [])

<!--
  List the categories of utterances that should NEVER be routed — backchannels,
  chatter, self-contained commentary, etc.  A good held-utterance list is
  critical: it prevents misroutes that annoy participants and distort the eval
  curves.
-->

- Backchannel acknowledgments: "yes", "got it", "okay", "heard", "on it".
- TODO: list other utterance types that are self-contained and not actionable.

## Urgency calibration

<!--
  Anchor the three urgency levels to concrete operational outcomes in your
  domain.  This is what the convener uses to assign urgency values.
-->

- **high** — TODO: what outcome makes it high urgency in your domain?
- **med**  — TODO: what outcome makes it medium urgency?
- **low**  — TODO: what outcome makes it low urgency?
