package oe.consumer;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.sql.SQLException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import org.apache.kafka.clients.consumer.Consumer;
import org.apache.kafka.clients.consumer.ConsumerRebalanceListener;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.ConsumerRecords;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.clients.consumer.OffsetAndMetadata;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.Producer;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.KafkaException;
import org.apache.kafka.common.PartitionInfo;
import org.apache.kafka.common.TopicPartition;
import org.apache.kafka.common.header.Header;
import org.apache.kafka.common.header.internals.RecordHeader;

/**
 * Reads question events from a Kafka topic and stores each one once in SQLite.
 *
 * The same guarantees as the Python consumer in this repository (consumer/kafka_source.py and
 * consumer/worker.py), in one Java class:
 *
 *   1. The offset is committed only AFTER the database write. A crash between the two means the
 *      message is delivered again (at-least-once) ...
 *   2. ... and the write is idempotent on event_id, so the second delivery changes nothing.
 *   3. A failure that may pass (the database is busy) is retried a bounded number of times:
 *      seek back to the message, pause the partition for the backoff, try again.
 *   4. A message that can never succeed (not JSON, a field missing), or one that has used up its
 *      attempts, goes to a dead-letter topic with the reason in its headers. Only when the broker
 *      has acknowledged that copy is the offset committed past the message.
 *   5. Configuration is checked before the first message: broker reachable, both topics exist.
 *
 * Exit codes: 0 clean; 1 something was dead-lettered or the broker failed mid-run;
 * 2 configuration; 3 only from the die-before-commit drill.
 */
public final class QuestionConsumer implements ConsumerRebalanceListener {

    private static final Duration POLL_TIMEOUT = Duration.ofMillis(500);

    private final Options options;
    private final Consumer<String, byte[]> consumer;
    private final Producer<String, byte[]> producer;
    private final EventStore store;
    // Anything after the JSON object makes the body invalid, as it does for the Python consumer.
    private final ObjectMapper json = new ObjectMapper().enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS);

    /** How many times each message has been tried, by its place: "topic[partition]@offset". */
    private final Map<String, Integer> attempts = new HashMap<>();
    /** Partitions paused for a retry, and the clock time (milliseconds) at which to resume each. */
    private final Map<TopicPartition, Long> pausedUntil = new HashMap<>();

    private boolean assigned = false;   // true once the group has given this consumer its partitions
    private long lastActivityMs = 0;    // when the last message was handled (or when partitions were assigned)
    private boolean drillFailureUsed = false;
    private int processed, duplicates, retried, deadLettered, malformed, committed;

    QuestionConsumer(Options options, Consumer<String, byte[]> consumer,
                     Producer<String, byte[]> producer, EventStore store) {
        this.options = options;
        this.consumer = consumer;
        this.producer = producer;
        this.store = store;
    }

    // ------------------------------------------------------------------ the program's entry point

    public static void main(String[] args) {
        Options options;
        try {
            options = Options.parse(args);
        } catch (IllegalArgumentException e) {
            System.err.println("configuration: " + e.getMessage());
            System.exit(2);
            return;   // never reached: System.exit stops the program; the compiler needs it
        }

        Properties consumerSettings = new Properties();
        consumerSettings.put("bootstrap.servers", options.bootstrap);
        consumerSettings.put("group.id", options.group);
        consumerSettings.put("enable.auto.commit", "false");       // this code commits, after the write
        consumerSettings.put("auto.offset.reset", "earliest");     // a new group starts at the beginning
        consumerSettings.put("max.poll.records", "1");             // one message in flight at a time
        consumerSettings.put("session.timeout.ms", "6000");        // a dead consumer is noticed in 6 s
        consumerSettings.put("allow.auto.create.topics", "false"); // a missing topic is an error
        consumerSettings.put("key.deserializer", "org.apache.kafka.common.serialization.StringDeserializer");
        consumerSettings.put("value.deserializer", "org.apache.kafka.common.serialization.ByteArrayDeserializer");

        Properties producerSettings = new Properties();
        producerSettings.put("bootstrap.servers", options.bootstrap);
        producerSettings.put("acks", "all");                       // the dead letter must be acknowledged
        producerSettings.put("request.timeout.ms", "5000");
        producerSettings.put("delivery.timeout.ms", "10000");      // give up on a send after 10 s, for good
        producerSettings.put("key.serializer", "org.apache.kafka.common.serialization.StringSerializer");
        producerSettings.put("value.serializer", "org.apache.kafka.common.serialization.ByteArraySerializer");

        int exitCode = -1;   // -1: the run has not produced a result yet
        // try-with-resources: all three are closed when the block ends, however it ends
        // (except Runtime.halt in the die-before-commit drill, which closes nothing: a crash).
        try (EventStore store = new EventStore(options.database);
             Consumer<String, byte[]> consumer = new KafkaConsumer<>(consumerSettings);
             Producer<String, byte[]> producer = new KafkaProducer<>(producerSettings)) {
            exitCode = new QuestionConsumer(options, consumer, producer, store).run();
        } catch (SQLException e) {
            if (exitCode == -1) {
                System.err.println("configuration: cannot open the database " + options.database + ": " + e.getMessage());
                exitCode = 2;
            } else {
                System.err.println("while closing the database: " + e.getMessage());
            }
        } catch (KafkaException e) {
            if (exitCode == -1) {
                System.err.println("configuration: cannot create the Kafka client: " + e.getMessage());
                exitCode = 2;
            } else {
                System.err.println("while closing the Kafka client: " + e.getMessage());
            }
        }
        System.exit(exitCode);
    }

    // ------------------------------------------------------------------ the loop

    int run() {
        String problem = configurationProblem();
        if (problem != null) {
            System.err.println("configuration: " + problem);
            return 2;
        }

        consumer.subscribe(List.of(options.topic), this);
        try {
            while (true) {
                resumePartitionsThatAreDue();
                ConsumerRecords<String, byte[]> records = consumer.poll(POLL_TIMEOUT);
                for (ConsumerRecord<String, byte[]> record : records) {
                    handle(record);
                    lastActivityMs = System.currentTimeMillis();
                }
                if (idleLongEnough()) {
                    break;
                }
            }
        } catch (KafkaException e) {
            // The broker went away, a commit failed, or a dead letter could not be sent. Nothing is
            // committed past an unprocessed message, so the next run picks up at the last commit.
            System.err.println("stopped: " + e.getMessage());
            logSummary(1);
            return 1;
        }

        int exitCode = deadLettered > 0 ? 1 : 0;
        logSummary(exitCode);
        return exitCode;
    }

    /** Broker reachable, both topics present. Returns null when everything is in place. */
    private String configurationProblem() {
        Map<String, List<PartitionInfo>> topics;
        try {
            topics = consumer.listTopics(Duration.ofSeconds(10));
        } catch (KafkaException e) {
            return "cannot reach the broker at " + options.bootstrap + ": " + e.getMessage();
        }
        if (!topics.containsKey(options.topic)) {
            return "topic " + options.topic + " does not exist (a consumer does not create it)";
        }
        if (!topics.containsKey(options.deadLetterTopic)) {
            return "dead-letter topic " + options.deadLetterTopic + " does not exist";
        }
        return null;
    }

    /** One message: parse, write, commit — or retry, or dead-letter. */
    private void handle(ConsumerRecord<String, byte[]> record) {
        TopicPartition partition = new TopicPartition(record.topic(), record.partition());
        String place = record.topic() + "[" + record.partition() + "]@" + record.offset();
        int attempt = attempts.getOrDefault(place, 0) + 1;     // 1 the first time, then 2, 3 ...
        attempts.put(place, attempt);

        Event event;
        try {
            event = parse(record.value());
        } catch (IllegalArgumentException e) {
            // Trying again cannot help: straight to the dead-letter topic.
            if (attempt == 1) {
                malformed++;
            }
            deadLetter(record, partition, place, attempt, "malformed: " + e.getMessage());
            return;
        }

        try {
            if ("fail-first-write".equals(options.drill) && !drillFailureUsed) {
                drillFailureUsed = true;
                throw new SQLException("drill: fail-first-write");
            }

            boolean stored = store.insert(event.id, event.question,
                                          record.topic(), record.partition(), record.offset());

            if ("die-before-commit".equals(options.drill)) {
                System.out.println("drill: die-before-commit: the row is written, the offset is not committed");
                System.out.flush();
                Runtime.getRuntime().halt(3);   // stop the process at once, as a crash would
            }

            commit(record, partition);          // only now: the write is in the database
            attempts.remove(place);
            if (stored) {
                processed++;
                log("processed", event.id, place, attempt, null);
            } else {
                duplicates++;
                log("duplicate", event.id, place, attempt, "already stored");
            }
        } catch (SQLException e) {
            if (attempt < options.maxAttempts) {
                long delayMs = 500;                             // 0.5 s, then 1 s, 2 s ...
                for (int i = 1; i < attempt; i++) {
                    delayMs = delayMs * 2;
                }
                retried++;
                retryLater(record, partition, delayMs);
                log("retry", event.id, place, attempt, e.getMessage() + "; retry in " + delayMs + " ms");
            } else {
                deadLetter(record, partition, place, attempt,
                           "failed after " + attempt + " attempts: " + e.getMessage());
            }
        }
    }

    /** The body must be a JSON object with a non-empty event_id and question. */
    private Event parse(byte[] body) {
        if (body == null) {
            throw new IllegalArgumentException("body is empty");
        }
        JsonNode node;
        try {
            node = json.readTree(body);
        } catch (IOException e) {
            throw new IllegalArgumentException("body is not JSON");
        }
        if (node == null || !node.isObject()) {
            throw new IllegalArgumentException("body is not a JSON object");
        }
        String id = text(node, "event_id");
        String question = text(node, "question");
        return new Event(id, question);
    }

    /** The named field as text; it must be there, be a string, and not be blank. */
    private static String text(JsonNode node, String field) {
        JsonNode value = node.get(field);
        if (value == null || !value.isTextual() || value.asText().isBlank()) {
            throw new IllegalArgumentException(field + " is missing or is not text");
        }
        return value.asText();
    }

    /** "Everything before offset + 1 on this partition is done." Synchronous: it waits for the broker. */
    private void commit(ConsumerRecord<String, byte[]> record, TopicPartition partition) {
        consumer.commitSync(Map.of(partition, new OffsetAndMetadata(record.offset() + 1)));
        committed++;
    }

    /** Go back to this message and leave the partition alone until the delay has passed. */
    private void retryLater(ConsumerRecord<String, byte[]> record, TopicPartition partition, long delayMs) {
        consumer.seek(partition, record.offset());
        consumer.pause(List.of(partition));
        pausedUntil.put(partition, System.currentTimeMillis() + delayMs);
    }

    private void resumePartitionsThatAreDue() {
        long now = System.currentTimeMillis();
        Iterator<Map.Entry<TopicPartition, Long>> paused = pausedUntil.entrySet().iterator();
        while (paused.hasNext()) {
            Map.Entry<TopicPartition, Long> entry = paused.next();
            if (entry.getValue() <= now) {
                consumer.resume(List.of(entry.getKey()));
                paused.remove();
            }
        }
    }

    /**
     * Copy the message to the dead-letter topic with the reason in its headers, wait for the
     * broker's acknowledgment, and only then commit past it. If the copy is not acknowledged the
     * run stops, with nothing committed. Kafka has no dead-letter queue of its own: this code
     * writes it.
     */
    private void deadLetter(ConsumerRecord<String, byte[]> record, TopicPartition partition,
                            String place, int attempt, String reason) {
        List<Header> headers = new ArrayList<>();
        headers.add(header("dlq.reason", reason));
        headers.add(header("dlq.source.topic", record.topic()));
        headers.add(header("dlq.source.partition", String.valueOf(record.partition())));
        headers.add(header("dlq.source.offset", String.valueOf(record.offset())));
        headers.add(header("dlq.attempts", String.valueOf(attempt)));

        try {
            producer.send(new ProducerRecord<>(options.deadLetterTopic, (Integer) null, record.key(), record.value(), headers))
                    .get(15, TimeUnit.SECONDS);   // wait for the broker's acknowledgment
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new KafkaException("interrupted while sending a dead letter for " + place);
        } catch (ExecutionException | TimeoutException | KafkaException e) {
            // Not acknowledged, so not committed. Stop the run: the message is still on the topic
            // and is delivered again to the next run.
            log("dead_letter_failed", null, place, attempt, String.valueOf(e.getMessage()));
            throw new KafkaException("dead letter not acknowledged for " + place + ": " + e.getMessage());
        }

        commit(record, partition);
        attempts.remove(place);
        deadLettered++;
        log("dead_lettered", null, place, attempt, reason);
    }

    private static Header header(String key, String value) {
        return new RecordHeader(key, value.getBytes(StandardCharsets.UTF_8));
    }

    /** A stream has no end: with --idle-exit N, stop after N quiet seconds with nothing waiting. */
    private boolean idleLongEnough() {
        if (options.idleExitSeconds <= 0 || !assigned || !pausedUntil.isEmpty()) {
            return false;
        }
        return System.currentTimeMillis() - lastActivityMs >= options.idleExitSeconds * 1000L;
    }

    // ------------------------------------------------------------------ the consumer group

    /** Called by the client, inside poll(), when the group gives this consumer partitions. */
    @Override
    public void onPartitionsAssigned(Collection<TopicPartition> partitions) {
        if (!assigned && !partitions.isEmpty()) {
            assigned = true;
            lastActivityMs = System.currentTimeMillis();   // the idle clock starts here
        }
    }

    /** Called when partitions are taken away: forget what was pending on them. */
    @Override
    public void onPartitionsRevoked(Collection<TopicPartition> partitions) {
        for (TopicPartition partition : partitions) {
            pausedUntil.remove(partition);
            String prefix = partition.topic() + "[" + partition.partition() + "]@";
            Iterator<String> places = attempts.keySet().iterator();
            while (places.hasNext()) {
                if (places.next().startsWith(prefix)) {
                    places.remove();
                }
            }
        }
    }

    // ------------------------------------------------------------------ logging

    /** One JSON line per outcome, on standard output. */
    private void log(String outcome, String eventId, String place, int attempt, String reason) {
        Map<String, Object> line = new LinkedHashMap<>();
        line.put("event", outcome);
        if (eventId != null) {
            line.put("event_id", eventId);
        }
        line.put("source", place);
        line.put("attempt", attempt);
        if (reason != null) {
            line.put("reason", reason);
        }
        print(line);
    }

    private void logSummary(int exitCode) {
        Map<String, Object> line = new LinkedHashMap<>();
        line.put("event", "summary");
        line.put("processed", processed);
        line.put("duplicates", duplicates);
        line.put("retried", retried);
        line.put("dead_lettered", deadLettered);
        line.put("malformed", malformed);
        line.put("committed", committed);
        line.put("exit", exitCode);
        print(line);
    }

    private void print(Map<String, Object> line) {
        try {
            System.out.println(json.writeValueAsString(line));
        } catch (IOException e) {
            System.out.println(line);
        }
    }

    // ------------------------------------------------------------------ small types

    /** What a valid message carries. */
    private static final class Event {
        final String id;
        final String question;

        Event(String id, String question) {
            this.id = id;
            this.question = question;
        }
    }

    /** The command-line options, with their defaults. */
    static final class Options {
        String bootstrap = "localhost:9092";
        String topic = "oe.questions";
        String deadLetterTopic;                 // default: <topic>.dlq
        String group = "oe-agent-java";
        String database = "java-drill.sqlite";
        int idleExitSeconds = 0;                // 0: run until Ctrl+C
        int maxAttempts = 3;
        String drill = "";                      // "", "die-before-commit" or "fail-first-write"

        static Options parse(String[] args) {
            Options options = new Options();
            for (int i = 0; i < args.length; i++) {
                String name = args[i];
                if (i + 1 >= args.length) {
                    throw new IllegalArgumentException(name + " needs a value");
                }
                String value = args[++i];
                switch (name) {
                    case "--bootstrap" -> options.bootstrap = value;
                    case "--topic" -> options.topic = value;
                    case "--dlq-topic" -> options.deadLetterTopic = value;
                    case "--group" -> options.group = value;
                    case "--db" -> options.database = value;
                    case "--idle-exit" -> options.idleExitSeconds = wholeNumber(name, value);
                    case "--max-attempts" -> options.maxAttempts = wholeNumber(name, value);
                    case "--drill" -> options.drill = value;
                    default -> throw new IllegalArgumentException("unknown option " + name);
                }
            }
            if (options.deadLetterTopic == null) {
                options.deadLetterTopic = options.topic + ".dlq";
            }
            if (options.maxAttempts < 1) {
                throw new IllegalArgumentException("--max-attempts must be 1 or more");
            }
            if (!List.of("", "die-before-commit", "fail-first-write").contains(options.drill)) {
                throw new IllegalArgumentException("--drill must be die-before-commit or fail-first-write");
            }
            return options;
        }

        private static int wholeNumber(String name, String value) {
            try {
                return Integer.parseInt(value);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(name + " must be a whole number, not " + value);
            }
        }
    }
}
