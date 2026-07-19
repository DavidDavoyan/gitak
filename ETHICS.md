# Data ethics

Gitak exists to help children, and a system that scores children can harm them if built carelessly. These rules are part of the design, and pull requests that weaken them will not be merged.

## 1. Public code, private data

This repository contains code and a synthetic-data generator only. Real student data must never be committed, published, or demoed publicly. A real deployment runs inside the school: the SQLite file stays on school infrastructure, and anything leaving it (reports, screenshots, research exports) must be anonymized or aggregated first.

## 2. The model advises, the teacher decides

A prediction is decision support, not a decision. Flags exist so a teacher can help a child earlier, with a reason written in plain language. No automatic consequence (grade, class reassignment, program placement, privilege) may be triggered by a model output alone. Class or program changes are pedagogical decisions made by humans who know the child.

## 3. The record belongs to the student

The lifetime transcript is the student's property. They (or their guardians while minors) can export it at any time, and what is shared with universities or employers later is their choice. The school is a custodian, not an owner.

## 4. Leaderboards motivate, they must not humiliate

Within-class scores are visible to that class and its teachers, and the score design guarantees a struggling student can climb the board by improving. Real deployments should keep leaderboards inside the class and celebrate the "most improved" list with the same weight as the top three.

## 5. Honesty about accuracy

Every model run stores its holdout error next to its predictions. If the error grows, the flags widen or the model is retired for that school until fixed. Never present a forecast as more certain than the stored error supports.

## 6. Children are not their worst quarter

History informs the model, but old flags must never follow a student as labels. A flag that did not repeat is evidence the system worked, and profile views should always lead with the trend, not the floor.
