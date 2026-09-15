package net.methadrenaline.bridge.velocity;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.junit.jupiter.api.Test;

class PublicAnnouncementSubscriptionTest {
    public static final class Provider {
        String language;
        Consumer<Component> listener;

        public AutoCloseable subscribePublicAnnouncements(String language, Consumer<Component> listener) {
            this.language = language;
            this.listener = listener;
            return () -> this.listener = null;
        }
    }

    @Test
    void forwardsRenderedPublicMessageOnceAndUnsubscribes() throws Exception {
        var provider = new Provider();
        var received = new ArrayList<String>();
        var subscription = PublicAnnouncementSubscription.subscribe(provider, "ru_ru", received::add);
        assertEquals("ru_ru", provider.language);
        provider.listener.accept(Component.text("PlayerOne", NamedTextColor.GOLD)
                .append(Component.text(" has connected", NamedTextColor.WHITE)));
        provider.listener.accept(Component.empty());
        assertEquals(List.of("PlayerOne has connected"), received);
        subscription.close();
        assertNull(provider.listener);
    }

    @Test
    void reportsUnsupportedProviderWithoutReceivingPrivateMessages() {
        assertThrows(NoSuchMethodException.class,
                () -> PublicAnnouncementSubscription.subscribe(new Object(), "en_us", text -> {}));
    }
}
