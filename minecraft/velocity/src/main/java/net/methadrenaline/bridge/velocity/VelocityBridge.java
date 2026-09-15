package net.methadrenaline.bridge.velocity;

import com.google.inject.Inject;
import com.velocitypowered.api.event.PostOrder;
import com.velocitypowered.api.event.Subscribe;
import com.velocitypowered.api.event.connection.PluginMessageEvent;
import com.velocitypowered.api.event.proxy.ProxyInitializeEvent;
import com.velocitypowered.api.event.proxy.ProxyShutdownEvent;
import com.velocitypowered.api.plugin.Dependency;
import com.velocitypowered.api.plugin.Plugin;
import com.velocitypowered.api.plugin.annotation.DataDirectory;
import com.velocitypowered.api.proxy.ProxyServer;
import com.velocitypowered.api.proxy.ServerConnection;
import java.io.ByteArrayInputStream;
import java.io.DataInputStream;
import java.nio.file.Path;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.minimessage.MiniMessage;
import net.kyori.adventure.text.minimessage.tag.resolver.Placeholder;
import net.methadrenaline.bridge.BridgeClient;
import net.methadrenaline.bridge.BridgeConfig;
import org.slf4j.Logger;

@Plugin(id = "chatbridge", name = "ChatBridge", version = "1.0.0", dependencies = {@Dependency(id = "mavelocore")})
public final class VelocityBridge {
    private final ProxyServer server;
    private final Logger logger;
    private final Path directory;
    private BridgeClient bridge;
    private BridgeConfig config;
    private AutoCloseable announcementSubscription;

    @Inject
    public VelocityBridge(ProxyServer server, Logger logger, @DataDirectory Path directory) {
        this.server = server;
        this.logger = logger;
        this.directory = directory;
    }

    @Subscribe
    public void start(ProxyInitializeEvent event) {
        try {
            config = BridgeConfig.load(directory.resolve("config.json"));
            if (!config.enabled) {
                return;
            }
            bridge = new BridgeClient(config, Runnable::run,
                    (username, text) -> server.getAllPlayers().forEach(player -> player.sendMessage(render(username, text))),
                    logger::info);
            bridge.start();
            subscribeAnnouncements();
        } catch (Exception exception) {
            logger.error("Chat bridge could not start: {}", exception.getClass().getSimpleName());
        }
    }

    private void subscribeAnnouncements() {
        if (!config.publicBroadcasts) {
            return;
        }
        try {
            Object provider = server.getPluginManager().getPlugin("mavelocore")
                    .flatMap(plugin -> plugin.getInstance()).orElseThrow();
            announcementSubscription = PublicAnnouncementSubscription.subscribe(
                    provider, config.publicAnnouncementLanguage, text -> bridge.publish("broadcast", text));
            logger.info("Public network announcements connected");
        } catch (ReflectiveOperationException | RuntimeException exception) {
            logger.warn("Public network announcements unavailable; MAVeloCore with announcement support is required: {}",
                    exception.getClass().getSimpleName());
        }
    }

    private Component render(String username, String text) {
        return MiniMessage.miniMessage().deserialize(
                config.discordFormat.replace("{username}", "<bridge_username>").replace("{text}", "<bridge_text>"),
                Placeholder.unparsed("bridge_username", username), Placeholder.unparsed("bridge_text", text));
    }

    @Subscribe(order = PostOrder.LAST)
    public void globalChat(PluginMessageEvent event) {
        if (bridge == null || !event.getIdentifier().getId().equals(config.globalChannel)
                || !(event.getSource() instanceof ServerConnection connection) || event.getResult().isAllowed()) {
            return;
        }
        try (DataInputStream input = new DataInputStream(new ByteArrayInputStream(event.getData()))) {
            if (!input.readUTF().equals(config.globalEvent)) {
                return;
            }
            String serverName = input.readUTF();
            String uuid = input.readUTF();
            String username = input.readUTF();
            String text = input.readUTF();
            if (!connection.getPlayer().getUniqueId().toString().equals(uuid)
                    || !connection.getPlayer().getUsername().equals(username)) {
                return;
            }
            bridge.publish("chat", "[" + serverName + "] " + username + " : " + text);
        } catch (Exception exception) {
            logger.warn("Could not read public chat event: {}", exception.getClass().getSimpleName());
        }
    }

    @Subscribe
    public void stop(ProxyShutdownEvent event) {
        if (announcementSubscription != null) {
            try {
                announcementSubscription.close();
            } catch (Exception exception) {
                logger.warn("Could not unsubscribe public network announcements: {}", exception.getClass().getSimpleName());
            }
        }
        if (bridge != null) {
            bridge.close();
        }
    }
}
