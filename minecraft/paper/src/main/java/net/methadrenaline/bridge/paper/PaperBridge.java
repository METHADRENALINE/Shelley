package net.methadrenaline.bridge.paper;

import io.papermc.paper.event.player.AsyncChatEvent;
import java.util.HashSet;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.minimessage.MiniMessage;
import net.kyori.adventure.text.minimessage.tag.resolver.Placeholder;
import net.kyori.adventure.text.serializer.plain.PlainTextComponentSerializer;
import net.methadrenaline.bridge.BridgeClient;
import net.methadrenaline.bridge.BridgeConfig;
import org.bukkit.Bukkit;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.server.BroadcastMessageEvent;
import org.bukkit.plugin.java.JavaPlugin;

public final class PaperBridge extends JavaPlugin implements Listener {
    private BridgeClient bridge;
    private final PlainTextComponentSerializer plain = PlainTextComponentSerializer.plainText();

    @Override
    public void onEnable() {
        try {
            BridgeConfig config = BridgeConfig.load(getDataFolder().toPath().resolve("config.json"));
            if (!config.enabled) {
                return;
            }
            bridge = new BridgeClient(config, action -> Bukkit.getScheduler().runTask(this, action),
                    (username, text) -> Bukkit.broadcast(MiniMessage.miniMessage().deserialize(
                            config.discordFormat.replace("{username}", "<bridge_username>").replace("{text}", "<bridge_text>"),
                            Placeholder.unparsed("bridge_username", username), Placeholder.unparsed("bridge_text", text))),
                    text -> getLogger().info(text));
            getServer().getPluginManager().registerEvents(this, this);
            bridge.start();
        } catch (Exception exception) {
            getLogger().severe("Chat bridge could not start: " + exception.getClass().getSimpleName());
            getServer().getPluginManager().disablePlugin(this);
        }
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onChat(AsyncChatEvent event) {
        var players = new HashSet<>(getServer().getOnlinePlayers());
        if (players.isEmpty() || !event.viewers().containsAll(players)) {
            return;
        }
        Component rendered = null;
        for (var viewer : players) {
            Component candidate = event.renderer().render(event.getPlayer(), event.getPlayer().displayName(), event.message(), viewer);
            if (rendered != null && !rendered.equals(candidate)) {
                return;
            }
            rendered = candidate;
        }
        if (rendered != null) {
            bridge.publish("chat", plain.serialize(rendered));
        }
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBroadcast(BroadcastMessageEvent event) {
        var players = new HashSet<>(getServer().getOnlinePlayers());
        if (!players.isEmpty() && event.getRecipients().containsAll(players)) {
            bridge.publish("broadcast", plain.serialize(event.message()));
        }
    }

    @Override
    public void onDisable() {
        if (bridge != null) {
            bridge.close();
        }
    }
}
