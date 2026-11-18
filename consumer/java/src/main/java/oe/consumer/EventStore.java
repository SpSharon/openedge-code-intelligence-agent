package oe.consumer;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Instant;

/**
 * The database side: one table, one row per event_id.
 *
 * The primary key on event_id and INSERT OR IGNORE make the write idempotent: writing the same
 * event twice leaves one row, and insert() says which of the two happened.
 */
final class EventStore implements AutoCloseable {

    private final Connection connection;

    EventStore(String path) throws SQLException {
        // A JDBC connection commits each statement as it runs (auto-commit), so when insert()
        // returns, the row is in the file.
        connection = DriverManager.getConnection("jdbc:sqlite:" + path);
        try (Statement statement = connection.createStatement()) {
            statement.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                    + " event_id TEXT PRIMARY KEY,"
                    + " question TEXT NOT NULL,"
                    + " topic TEXT NOT NULL,"
                    + " partition_no INTEGER NOT NULL,"
                    + " offset_no INTEGER NOT NULL,"
                    + " stored_at TEXT NOT NULL)");
        }
    }

    /** Returns true if the row was written now, false if this event_id was already stored. */
    boolean insert(String eventId, String question, String topic, int partition, long offset)
            throws SQLException {
        String sql = "INSERT OR IGNORE INTO events"
            + " (event_id, question, topic, partition_no, offset_no, stored_at) VALUES (?, ?, ?, ?, ?, ?)";
        try (PreparedStatement insert = connection.prepareStatement(sql)) {
            insert.setString(1, eventId);
            insert.setString(2, question);
            insert.setString(3, topic);
            insert.setInt(4, partition);
            insert.setLong(5, offset);
            insert.setString(6, Instant.now().toString());
            return insert.executeUpdate() == 1;
        }
    }

    @Override
    public void close() throws SQLException {
        connection.close();
    }
}
