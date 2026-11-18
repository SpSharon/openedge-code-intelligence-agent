# The question consumer in Java

The Kafka side of this repository's event consumer, a second time, in Java: read question events
from a topic, store each one once in SQLite, commit the offset only after the write, retry what
may pass, dead-letter what cannot. It is here to be read next to the Python version
(`consumer/kafka_source.py`, `consumer/worker.py`) — the same guarantees, the same three drills,
the same event files — and it does **not** call the agent: where the Python worker asks the model
and stores the answer, this one stores the question. Two classes, about 530 lines with their
comments.

| File | What it is |
|---|---|
| `pom.xml` | The Maven build: the Kafka client, Jackson (JSON), the SQLite JDBC driver, and one runnable jar |
| `src/main/java/oe/consumer/QuestionConsumer.java` | The program: settings, the poll loop, parse → write → commit, retry, dead letter, the log lines |
| `src/main/java/oe/consumer/EventStore.java` | The database: one table, `INSERT OR IGNORE` on `event_id` |
| `src/main/resources/simplelogger.properties` | Keeps the Kafka client's own logging to warnings |

**Status.** Written by an AI coding agent to Sharon Paul's specification, and reviewed in a fresh
context before delivery. Until her run it had been compiled and exercised only against stand-ins
for the Kafka client and the database, which cannot show that the real client and a real broker
behave the same way. Sharon Paul compiled it and ran it on 17 Nov 2026 on her own machine
(Windows; compiled with the `maven:3.9-eclipse-temurin-21` image, the first compile against the
real client; run on Temurin 21.0.13; `apache/kafka:4.0.0` in Docker), all three drills, with the
outcomes the table below expects: drill 1 — processed 2, duplicates 1, dead-lettered 1,
malformed 1, committed 4, exit 1, lag 0, and the dead letter read back from `oe.questions.dlq`
with its reason and source offset in the headers; drill 2 — exit 3 after the write, the group at
offset 4 of 5 (lag 1), then the same offset delivered again, logged `duplicate` / `already
stored`, committed, lag 0; drill 3 — a `retry` at attempt 1, then `processed` at attempt 2 on the
same offset, lag 0. One local broker, one partition, each drill once: that is all the run shows.
The rows in the SQLite file were not inspected; the single write is inferred from the `duplicate`
/ `already stored` line. The failure paths outside the drills (a missing topic, a dead letter the
broker does not acknowledge, attempts used up) have been run only against the stand-ins, not
against a real broker.

## Build

Running it needs a Java runtime, version 17 or later. Building it needs Maven; without installing
Maven, its Docker image does the same (PowerShell, from the repository root; the named volume
keeps the downloaded libraries between builds; `-q` means it prints nothing unless something
fails, and the first build downloads the libraries, which takes a minute or two):

```powershell
docker run --rm -v "${PWD}/consumer/java:/app" -v oe-m2:/root/.m2 -w /app maven:3.9-eclipse-temurin-21 mvn -q -B package
```

or, with Maven installed: `mvn -q -B -f consumer/java/pom.xml package`. Either leaves
`consumer/java/target/oe-question-consumer.jar` (ignored by git).

## Run

The broker, the topics and the producer are the ones in the main README ("The same worker on
Kafka"):

```powershell
docker compose -f consumer/docker-compose.kafka.yml up -d
docker exec oe-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --create --if-not-exists --topic oe.questions
docker exec oe-kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --create --if-not-exists --topic oe.questions.dlq
python -m consumer.kafka_produce --topic oe.questions --input consumer/drills/1-mixed-batch.jsonl
java -jar consumer/java/target/oe-question-consumer.jar --topic oe.questions --db java-drill.sqlite --idle-exit 15
```

| Option | Default | Meaning |
|---|---|---|
| `--bootstrap` | `localhost:9092` | The broker |
| `--topic` | `oe.questions` | The topic to read |
| `--dlq-topic` | `<topic>.dlq` | Where a message that cannot be processed goes |
| `--group` | `oe-agent-java` | The consumer group — its own, so it does not share offsets with the Python consumer |
| `--db` | `java-drill.sqlite` | The SQLite file |
| `--idle-exit N` | off | Stop after N quiet seconds, counted from the moment the group has assigned partitions |
| `--max-attempts` | `3` | Tries before a failing write is dead-lettered |
| `--drill` | none | `die-before-commit` or `fail-first-write` — for the drills only |

Every option takes a value; there is no `--help`. Without `--idle-exit` the run goes on until
Ctrl+C, which stops it without a summary line.

Exit codes: 0 clean · 1 something was dead-lettered, or the broker failed mid-run · 2 configuration
(broker unreachable, a topic missing, the database cannot be opened, a bad option) · 3 only from
`--drill die-before-commit`. A dead letter that the broker does not acknowledge stops the run
(exit 1) with nothing committed past the message, so the next run meets it again.

## The three drills

A fresh broker and a fresh database, as for the Python drills.

| Drill | Do | Expect |
|---|---|---|
| 1 · A mixed batch | produce `1-mixed-batch.jsonl`; run with `--idle-exit 15` | `processed 2 · duplicates 1 · dead_lettered 1 · malformed 1 · committed 4`, exit 1; one message on `oe.questions.dlq` with the reason and the source offset in its headers |
| 2 · A crash between the write and the commit | produce `2-crash-before-commit.jsonl`; run with `--drill die-before-commit`; run again without the flag, with `--idle-exit 15` | the first run exits with code 3 after the write; the second waits up to about six seconds for the dead member's session to expire, then logs `duplicate` / `already stored` for the same offset and commits it |
| 3 · A transient failure | produce `3-transient-failure.jsonl`; run with `--drill fail-first-write` | a `retry` line at attempt 1, then `processed` at attempt 2 on the same offset |

The group's position and lag, from the broker:

```powershell
docker exec oe-kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --group oe-agent-java
```

## What it is not

One thread and no framework, for reading: no Spring, no schema registry, no security settings, JSON
bodies only, no unit tests of its own (the Kafka client ships `MockConsumer` and `MockProducer`
for that; they are not used here). One message is in flight at a time (`max.poll.records=1`), so
it is slow by design. The attempt count lives in the process and starts again at 1 after a
restart; a rebalance forgets a pending retry delay. It has no production use and makes no claim
about one.
