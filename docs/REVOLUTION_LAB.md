# Revolution Lab: autonomous travel-agent experiments

Revolution Lab is a distinct development line with a product-first direction: meaningful capabilities for the traveller, not just more tests and taskbook receipts.

Areas explored include journey planning with AI tools, city/provider fact acquisition, proactive context checks, bounded recovery proposals, group consent and rendezvous handling, shared place discovery and short two-stop walking experiences.

## Representative evidence

- R103 describes a signed, two-place public discovery and sharing flow. Its controlled Chromium run used loopback `TEST_PROVIDER` fixtures. It does not establish live global service performance, real arrival or a booking transaction.
- R108 describes a Pocket Walk GO MODE that persists member-private, *self-reported* progression. It explicitly does not prove physical arrival. A documented controlled run reported two passing browser tests with AI disabled.
- R109 records design and development of a second-stop pivot. Presence of a taskbook is not proof of a completed deployment.

## Source boundaries
The code under `experiments/revolution/` is a **separate branch snapshot** based on `gpt/revolution-r33-open-world-journey-adaptation` (commit `a5a2f1096e0fbd806acd8be134fe8fb6095ad1a9`). The main classroom-code snapshot is based on `coursework/part2-20261104-kickoff` (commit `9115b6521678d587c2dea1cee0b20b08413854a0`). No combined build is claimed.

The North Star was an agent that can understand a traveller's goal, build and maintain a journey, watch for relevant changes, seek required approval and use tools to continue the trip. This remains a research direction rather than a proven deployed claim.
