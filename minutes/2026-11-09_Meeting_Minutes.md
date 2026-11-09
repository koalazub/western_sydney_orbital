# 2026-11-09_Meeting_Minutes.md

## **Camera Use and Orientation**

Camera seen as last-second emergency avoidance, possibly a tiebreaker in grading; OpenCV tracking easy at exposure 15

Nic flips the shuttle before burns to use rear thrusters, which have the best ISP and let cameras face forward

Tom suggested a small kick and drift for rotation to save fuel; Ali noted coasting burns may be less efficient

## **Logging and Sharing Results**

Submission needs a recording plus log files covering movement, fuel, hits, damage, score and time

Default logs only record init, hits and run end; adding custom telemetry is encouraged

Members should share log, recording and reasoning when they believe they cracked something important

## **Next Steps**

(Naqib) Pin server and Tailscale access details on Discord and add Ali to the network

(Naqib) Follow up on Discord to collect everyone's latest MVPs for benchmarking on different devices

(Nic) Post a high-level review of Tom's fork to Discord within about 18 hours, then walk through it in person

(Naqib) Share meeting notes and make local recordings of future meetings

(Ali & Bui) Redo MVP based on Tom's fork and push it; share the AlternaSim repo link

(Harry) Run everyone's MVPs headless with multiple seeds Thursday evening and Friday, report score and radius error spread

(Harry) Share the log analysis methodology code in a form others can run on their own logs

(Dominic) Prototype the shortest camera-based emergency avoidance maneuver, flagging how often it triggers

## **Decisions Made**

Tom's code is the common base; Tom's repo is the source of truth for competition submission

Code rule: one file besides the controller script, under 200 lines excluding comments and whitespace

Build radar-only MVP first; camera integration only later if time allows
