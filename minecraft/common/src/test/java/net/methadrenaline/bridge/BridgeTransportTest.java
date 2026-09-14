package net.methadrenaline.bridge;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.Gson;
import com.sun.net.httpserver.HttpsConfigurator;
import com.sun.net.httpserver.HttpsServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class BridgeTransportTest {
    @TempDir
    Path directory;

    @Test
    void encryptedTransportRetriesDeduplicatesAndDoesNotEcho() throws Exception {
        Path keytool = Path.of(System.getProperty("java.home"), "bin", System.getProperty("os.name").startsWith("Windows") ? "keytool.exe" : "keytool");
        Path keystore = directory.resolve("server.p12");
        Path certificate = directory.resolve("server.pem");
        Process generate = new ProcessBuilder(keytool.toString(), "-genkeypair", "-alias", "server",
                "-keystore", keystore.toString(), "-storetype", "PKCS12", "-storepass", "testonly",
                "-keyalg", "EC", "-dname", "CN=localhost", "-ext", "SAN=dns:localhost,ip:127.0.0.1", "-validity", "1")
                .redirectErrorStream(true).redirectOutput(ProcessBuilder.Redirect.DISCARD).start();
        assertEquals(0, generate.waitFor());
        Process export = new ProcessBuilder(keytool.toString(), "-exportcert", "-rfc", "-alias", "server",
                "-keystore", keystore.toString(), "-storepass", "testonly", "-file", certificate.toString())
                .redirectErrorStream(true).redirectOutput(ProcessBuilder.Redirect.DISCARD).start();
        assertEquals(0, export.waitFor());
        KeyStore keys = KeyStore.getInstance("PKCS12");
        try (var input = Files.newInputStream(keystore)) {
            keys.load(input, "testonly".toCharArray());
        }
        KeyManagerFactory manager = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
        manager.init(keys, "testonly".toCharArray());
        SSLContext tls = SSLContext.getInstance("TLS");
        tls.init(manager.getKeyManagers(), null, null);
        HttpsServer server = HttpsServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setHttpsConfigurator(new HttpsConfigurator(tls));
        Gson gson = new Gson();
        String token = UUID.randomUUID().toString() + UUID.randomUUID();
        AtomicInteger calls = new AtomicInteger();
        AtomicInteger deliveries = new AtomicInteger();
        AtomicInteger outgoing = new AtomicInteger();
        AtomicReference<String> authorization = new AtomicReference<>();
        CountDownLatch complete = new CountDownLatch(1);
        server.createContext("/exchange", exchange -> {
            authorization.set(exchange.getRequestHeaders().getFirst("Authorization"));
            var request = gson.fromJson(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8), BridgeClient.Exchange.class);
            if (calls.incrementAndGet() == 1) {
                exchange.sendResponseHeaders(503, -1);
                exchange.close();
                return;
            }
            outgoing.addAndGet(request.events().size());
            var reply = new BridgeClient.Reply(request.events().stream().map(BridgeClient.Event::id).toList(),
                    List.of(new BridgeClient.Message("88", "discord_user", "hello")));
            byte[] body = gson.toJson(reply).getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
            if (calls.get() >= 4 && request.acknowledged().contains("88")) {
                complete.countDown();
            }
        });
        server.start();
        BridgeConfig config = new BridgeConfig();
        config.enabled = true;
        config.endpoint = "https://localhost:" + server.getAddress().getPort() + "/exchange";
        config.certificateFile = certificate.toString();
        config.tokenFile = directory.resolve("token").toString();
        Files.writeString(Path.of(config.tokenFile), token);
        config.pollMillis = 200;
        config.validate();
        AtomicReference<BridgeClient> reference = new AtomicReference<>();
        try (BridgeClient client = new BridgeClient(config, Runnable::run, (username, text) -> {
            assertEquals("discord_user", username);
            assertEquals("hello", text);
            deliveries.incrementAndGet();
            reference.get().publish("broadcast", "must not echo");
        }, text -> {})) {
            reference.set(client);
            client.publish("chat", "public");
            client.start();
            assertTrue(complete.await(15, TimeUnit.SECONDS));
            assertEquals(1, deliveries.get());
            assertEquals(1, outgoing.get());
            assertEquals("Bearer " + token, authorization.get());
        } finally {
            server.stop(0);
        }
    }
}
