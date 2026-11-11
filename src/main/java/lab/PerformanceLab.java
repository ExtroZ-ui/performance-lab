package lab;

import jakarta.annotation.PreDestroy;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

@SpringBootApplication
@RestController
public class PerformanceLab {
    private final ExecutorService platform;
    private final ExecutorService virtual = Executors.newThreadPerTaskExecutor(
            Thread.ofVirtual().name("lab-virtual-", 0).factory());
    private final long waitMillis;

    public PerformanceLab(@Value("${lab.platform-workers}") int platformWorkers,
                          @Value("${lab.wait-ms}") long waitMillis) {
        platform = Executors.newFixedThreadPool(platformWorkers,
                Thread.ofPlatform().name("lab-platform-", 0).factory());
        this.waitMillis = waitMillis;
    }

    public static void main(String[] args) {
        SpringApplication.run(PerformanceLab.class, args);
    }

    @GetMapping("/work/platform")
    public CompletableFuture<Map<String, Boolean>> platformWork() {
        return CompletableFuture.supplyAsync(this::simulateBlockingIo, platform);
    }

    @GetMapping("/work/virtual")
    public CompletableFuture<Map<String, Boolean>> virtualWork() {
        return CompletableFuture.supplyAsync(this::simulateBlockingIo, virtual);
    }

    private Map<String, Boolean> simulateBlockingIo() {
        try {
            Thread.sleep(waitMillis);
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Work interrupted", exception);
        }
        return Map.of("virtual", Thread.currentThread().isVirtual());
    }

    @PreDestroy
    void shutdown() {
        platform.shutdownNow();
        virtual.shutdownNow();
    }
}
