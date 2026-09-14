package net.methadrenaline.bridge;

import com.google.gson.Gson;
import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.security.cert.CertificateFactory;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import java.util.function.BiConsumer;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;

public final class BridgeClient implements AutoCloseable {
    public record Event(String id, String timestamp, String kind, String text) {}
    public record Message(String id, String username, String text) {}
    public record Exchange(List<Event> events, List<String> acknowledged) {}
    public record Reply(List<String> accepted, List<Message> messages) {}
    private static final ThreadLocal<Boolean> RELAYING = ThreadLocal.withInitial(() -> false);
    private final BridgeConfig config;
    private final Consumer<Runnable> dispatch;
    private final BiConsumer<String, String> broadcast;
    private final Consumer<String> log;
    private final Gson gson = new Gson();
    private final ArrayDeque<Event> outgoing = new ArrayDeque<>();
    private final LinkedHashMap<String, Instant> received = new LinkedHashMap<>();
    private final List<String> acknowledged = new ArrayList<>();
    private final ScheduledExecutorService worker;
    private final HttpClient http;
    private final String token;
    private boolean connected;
    private long lastWarning;

    public BridgeClient(BridgeConfig config, Consumer<Runnable> dispatch, BiConsumer<String, String> broadcast, Consumer<String> log) throws Exception {
        this.config = config;
        this.dispatch = dispatch;
        this.broadcast = broadcast;
        this.log = log;
        this.token = Files.readString(Path.of(config.tokenFile)).strip();
        if (token.length() < 32 || token.contains("\n") || token.contains("\r")) {
            throw new IllegalArgumentException("Invalid bridge token file");
        }
        KeyStore trusted = KeyStore.getInstance(KeyStore.getDefaultType());
        trusted.load(null);
        try (InputStream stream = Files.newInputStream(Path.of(config.certificateFile))) {
            trusted.setCertificateEntry("bridge", CertificateFactory.getInstance("X.509").generateCertificate(stream));
        }
        TrustManagerFactory factory = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        factory.init(trusted);
        SSLContext tls = SSLContext.getInstance("TLS");
        tls.init(null, factory.getTrustManagers(), null);
        this.http = HttpClient.newBuilder().sslContext(tls).connectTimeout(Duration.ofSeconds(config.requestTimeoutSeconds)).build();
        this.worker = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "chat-bridge");
            thread.setDaemon(true);
            return thread;
        });
    }

    public void start() {
        worker.scheduleWithFixedDelay(this::exchange, 0, config.pollMillis, TimeUnit.MILLISECONDS);
    }

    public void publish(String kind, String text) {
        if (RELAYING.get() || !config.enabled || (!config.playerChat && kind.equals("chat"))
                || (!config.publicBroadcasts && kind.equals("broadcast"))) {
            return;
        }
        String cleaned = clean(text, config.maxMessageLength);
        if (cleaned.isBlank()) {
            return;
        }
        synchronized (outgoing) {
            if (outgoing.size() >= config.maxQueue) {
                warn("chat bridge outgoing queue is full");
                return;
            }
            outgoing.add(new Event(UUID.randomUUID().toString(), Instant.now().toString(), kind, cleaned));
        }
    }

    public static String clean(String text, int limit) {
        if (text == null) {
            return "";
        }
        StringBuilder result = new StringBuilder();
        text.replaceAll("(?i)§[0-9a-fk-orx]", "").codePoints()
                .filter(c -> c == '\n' || (!Character.isISOControl(c) && Character.getType(c) != Character.FORMAT))
                .limit(limit).forEach(result::appendCodePoint);
        return result.toString().strip();
    }

    public static String format(String template, String username, String text) {
        String name = clean(username, 80).replace("\n", " ");
        return template.replace("{username}", name).replace("{text}", text);
    }

    private void exchange() {
        try {
            List<Event> events;
            synchronized (outgoing) {
                outgoing.removeIf(event -> Instant.parse(event.timestamp()).plusSeconds(config.messageTtlSeconds).isBefore(Instant.now()));
                events = outgoing.stream().limit(25).toList();
            }
            HttpRequest request = HttpRequest.newBuilder(URI.create(config.endpoint))
                    .timeout(Duration.ofSeconds(config.requestTimeoutSeconds))
                    .header("Authorization", "Bearer " + token).header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(gson.toJson(new Exchange(events, List.copyOf(acknowledged))))).build();
            HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 200) {
                warn("chat bridge HTTP status " + response.statusCode());
                connected = false;
                return;
            }
            Reply reply = gson.fromJson(response.body(), Reply.class);
            if (reply == null || reply.accepted() == null || reply.messages() == null || reply.messages().size() > 25) {
                throw new IllegalArgumentException("Invalid bridge reply");
            }
            synchronized (outgoing) {
                Set<String> accepted = Set.copyOf(reply.accepted());
                outgoing.removeIf(event -> accepted.contains(event.id()));
            }
            acknowledged.clear();
            for (Message message : reply.messages()) {
                if (message.id() == null || message.username() == null || message.text() == null) {
                    continue;
                }
                CompletableFuture<Void> delivered = new CompletableFuture<>();
                dispatch.accept(() -> {
                    try {
                        received.entrySet().removeIf(entry -> entry.getValue().plusSeconds(config.messageTtlSeconds * 2L).isBefore(Instant.now()));
                        if (!received.containsKey(message.id())) {
                            RELAYING.set(true);
                            try {
                                broadcast.accept(clean(message.username(), 80).replace("\n", " "), clean(message.text(), config.maxMessageLength));
                                received.put(message.id(), Instant.now());
                            } finally {
                                RELAYING.remove();
                            }
                        }
                        delivered.complete(null);
                    } catch (Exception exception) {
                        delivered.completeExceptionally(exception);
                    }
                });
                delivered.get(config.requestTimeoutSeconds, TimeUnit.SECONDS);
                acknowledged.add(message.id());
            }
            if (!connected) {
                log.accept("chat bridge connected");
                connected = true;
            }
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
        } catch (Exception exception) {
            connected = false;
            warn("chat bridge exchange failed: " + exception.getClass().getSimpleName());
        }
    }

    private void warn(String message) {
        long now = System.nanoTime();
        if (now - lastWarning > TimeUnit.SECONDS.toNanos(60)) {
            log.accept(message);
            lastWarning = now;
        }
    }

    @Override
    public void close() {
        worker.shutdownNow();
        http.close();
    }
}
