# Review Evidence Inventory — 2026-07-15

## Target Production Run

- Run: `production_full_0276b82e-3dc5-445e-8758-e2de527ea216`
- Source: `python_backend/data/raw5760x144020fps.mp4`
- Accepted trial: `production_trial_e3647dbc-59ec-400a-84f1-b19590086a41`
- Locked configuration SHA-256: `6fd624d76b2688982f3fd18922d6aaf16d519f3da598b653f44d62da9a5fda4c`
- Source video SHA-256: `0921c68ad1a1c0d35dd2d70f5f8352c52d11ca41f8e1f2a72af9e393eb9c3986`
- Tracking contract SHA-256: `ff2cd5e6c6477712543e5cedc1bcfe82c18d3e55e49ad74601d5d2aa1b4a591b`
- Action-signal report SHA-256: `16e292a9f361c36fa07292892429dcb4cbb53f48aeb49fc9dbd703464c5f773f`
- Decodable frames: 5,192 of 5,194 reported at 20 fps
- Candidate count: 6,406
- Confirmed classifications: 0
- Selective decisions: 0

The two-frame terminal decoder shortfall was independently acknowledged. Its evidence SHA-256 is `fa331352742f84a0fc1831abfeb26040df3fa7ff1db45f366792119107c55c4b`; acknowledgement SHA-256 is `b964b2ef9a3c22d4660222642731ed9b5331671663c1028e284c0ac6152ad01e`. This acknowledgement does not remove review-evidence or trajectory blockers.

## Local Source-Group Inventory

The three local MP4 files are alternate encodes, resolutions, clips, or derivatives of one logical match. They must remain one source group:

- `BXZFAuu1GQo_20260629_YRSL_U13B_LSSC_vs_DSC_PANO_5120x1440_hevc.mp4`
- `BXZFAuu1GQo_20260629_YRSL_U13B_LSSC_vs_DSC_PANO_7680x2160.mp4`
- `raw5760x144020fps.mp4`

Available independent match groups: 1. The classifier requires at least 3 genuinely independent source groups for leakage-safe train/calibration/test and therefore cannot be qualified from the current inventory.

## Missing Evidence

No real artifact was found for:

- candidate dataset manifest;
- blind primary vote ledgers or adjudication;
- annotation-resolution report;
- model manifest, weights, or training report;
- target predictions;
- policy-role assignment;
- qualified selective policy or decisions;
- self-contained review-evidence bundle; or
- `selective_review_queue.v1.json`.

## Policy Feasibility Finding

The current policy component contract groups all candidates sharing a video SHA-256 and admits one evaluation candidate per connected component. With the existing exact statistical gates, the available scale cannot qualify the policy. Depending on the frozen threshold-hypothesis count, the evidence review estimated hundreds to more than one thousand independent true-ball videos could be required.

PR4A does not lower this contract. A separate statistical-contract defect review and substantially larger independent evidence inventory are required before training a new qualified policy, unless a genuinely qualified external package is supplied.

## Target Tracking Risks Beyond Candidate Classification

- Lost frames: 1,549
- Long lost gaps: 147 frames and 282 frames
- Suspicious tracklets: 72 of 78
- Maximum observed jump: approximately 4,114 pixels

Even exhaustive candidate labeling would not by itself prove missed-ball safety. The long gaps, jumps, reacquisition, and resulting trajectory require independent frame/trajectory review or a separately approved human trajectory-correction contract.

## Resource Gates

The long-term ML route needs:

- at least 3 independent matches, with 6–10 recommended for initial scene/class coverage;
- two blind primary reviewers and a separate adjudicator;
- a frozen protocol and unobserved audit population;
- sufficient label support for match-ball and noise in every required population;
- a reviewed fix for the current policy inferential-unit feasibility problem;
- storage for three evidence populations, staging, checkpoints, model packages, media, and safety margin; and
- an independent activation auditor.

The alternative exhaustive-human route is not part of PR4A. It would require at least 12,812 blind primary votes for 6,406 candidates, roughly 53–107 primary-review hours at 15–30 seconds per vote, plus adjudication/QA and separate trajectory-gap review.

## Current Decision

Proceed with the fail-closed PR4A bundle/import shell and deterministic fixture. The real target must remain blocked, with the root queue facade absent, until a qualified bundle exists and passes activation plus independent audit.
