package com.example.archmind;

import com.example.archmind.dao.neo4j.CodeGraphNeo4jWriter;
import com.example.archmind.model.ast.ParsedProject;
import com.example.archmind.model.ast.SourceFileView;
import com.example.archmind.service.ast.AstParserService;
import com.example.archmind.service.domain.BusinessArea;
import com.example.archmind.service.domain.BusinessAreaResolver;
import com.example.archmind.service.domain.Capability;
import com.example.archmind.service.domain.CapabilityResolver;
import com.example.archmind.service.domain.DomainModel;
import com.example.archmind.service.domain.DomainModelValidator;
import com.example.archmind.service.domain.GraphSnapshot;
import com.example.archmind.service.domain.GraphSnapshotDao;
import com.example.archmind.service.domain.Role;
import com.example.archmind.service.domain.RoleClassifier;
import com.example.archmind.service.domain.ValidationReport;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Session;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * R8 手动集成冒烟（真实项目版）：把 test-demos 下的示例工程真解析、真写远端 Neo4j，
 * 再跑一遍领域建模，打印每个项目的领域/能力结果。跑完删除所有临时数据。
 *
 * <pre>./mvnw -Dtest=R8RealProjectIT -Dr8.smoke=true test</pre>
 */
@SpringBootTest
class R8RealProjectIT {

    private static final Path DEMOS = Path.of("..", "test-demos", "src");
    private static final long PID_BASE = 987654400L;

    @Autowired
    private AstParserService astParserService;
    @Autowired
    private CodeGraphNeo4jWriter graphWriter;
    @Autowired
    private GraphSnapshotDao snapshotDao;
    @Autowired
    private Driver driver;

    @BeforeEach
    void requireExplicitOptIn() {
        Assumptions.assumeTrue(Boolean.getBoolean("r8.smoke"),
                "需要 -Dr8.smoke=true 才执行远端写入冒烟");
        Assumptions.assumeTrue(Files.isDirectory(DEMOS), "找不到 test-demos/src");
    }

    @Test
    void analyzeEveryDemoProject() throws Exception {
        String explicit = System.getProperty("r8.project");
        if (explicit != null && !explicit.isBlank()) {
            runOne(Path.of(explicit), PID_BASE);
            return;
        }

        List<Path> projects = new ArrayList<>();
        try (var stream = Files.list(DEMOS)) {
            stream.filter(Files::isDirectory).sorted().forEach(projects::add);
        }

        for (int i = 0; i < projects.size(); i++) {
            runOne(projects.get(i), PID_BASE + i);
        }
    }

    private void runOne(Path project, long projectId) throws Exception {
        clean(projectId);
        try {
            ParsedProject parsed = astParserService.parse(readSources(project));
            graphWriter.replaceProjectGraph(projectId, parsed);

            GraphSnapshot snapshot = snapshotDao.load(projectId);
            Map<String, RoleClassifier.Classified> roles = new RoleClassifier().classify(snapshot.classes());
            BusinessAreaResolver.AreaResult areas =
                    new BusinessAreaResolver().resolve(snapshot.classes(), roles, project.getFileName().toString());
            Capability.CapabilityResult caps = new CapabilityResolver().resolve(
                    snapshot.endpoints(), areas.classToArea(), roles, snapshot.calls());

            DomainModel model = new DomainModel(areas.areas(), caps.capabilities(), caps.areaCalls(),
                    areas.classToArea(), roles);
            ValidationReport report = new DomainModelValidator().validate(model, snapshot.classes().size());

            assertEquals(snapshot.classes().size(), roles.size(), project + " role 未覆盖全部类");
            assertEquals(snapshot.classes().size(), areas.classToArea().size(), project + " 领域未覆盖全部类");

            System.out.printf("%n=== %s ===%n", project.getFileName());
            System.out.printf("classes=%d edges=%d endpoints=%d strategy=%s%n",
                    snapshot.classes().size(), parsed.getEdges().size(), snapshot.endpoints().size(),
                    areas.strategy());
            System.out.printf("roles=%s%n", countRoles(roles));
            System.out.printf("areas=%s%n", areas.areas().stream()
                    .map(a -> a.uid() + "(" + a.kind() + ")").toList());
            System.out.printf("capabilities=%s%n", caps.capabilities().stream()
                    .map(Capability::uid).toList());
            if (System.getProperty("r8.project") != null) {
                System.out.printf("endpoints=%s%n", snapshot.endpoints().stream()
                        .map(e -> e.uid() + " -> " + areas.classToArea().get(owner(e.handlerUid())))
                        .toList());
                List<com.example.archmind.service.domain.GraphSnapshot.ClassSnapshot> all = snapshot.classes();
                int step = Math.max(1, all.size() / 20);
                for (int i = 0, n = 0; i < all.size() && n < 20; i += step, n++) {
                    var cls = all.get(i);
                    System.out.printf("sample %s | %s | %s%n", cls.uid(),
                            roles.get(cls.uid()) == null ? "?" : roles.get(cls.uid()).role(),
                            areas.classToArea().get(cls.uid()));
                }
            }
            System.out.printf("areaCalls=%d validation=%s%s warnings=%s%n", caps.areaCalls().size(),
                    report.ok() ? "OK" : "FAIL", report.ok() ? "" : " " + report.failures(),
                    report.warnings());
        } finally {
            clean(projectId);
        }
    }

    private Map<String, Long> countRoles(Map<String, RoleClassifier.Classified> roles) {
        Map<String, Long> out = new LinkedHashMap<>();
        for (Role role : Role.values()) {
            long n = roles.values().stream().filter(c -> c.role() == role).count();
            if (n > 0) {
                out.put(role.name(), n);
            }
        }
        return out;
    }

    private List<SourceFileView> readSources(Path project) throws Exception {
        List<SourceFileView> views = new ArrayList<>();
        try (var stream = Files.walk(project)) {
            List<Path> files = stream.filter(p -> p.toString().endsWith(".java")).sorted().toList();
            // 有标准 Maven 布局时只看主源码，跳过测试代码
            if (files.stream().anyMatch(R8RealProjectIT::isMainSource)) {
                files = files.stream().filter(R8RealProjectIT::isMainSource).toList();
            }
            long id = 1;
            for (Path file : files) {
                views.add(new SourceFileView(id++, project.relativize(file).toString().replace('\\', '/'),
                        Files.readString(file)));
            }
        }
        return views;
    }

    private static String owner(String methodUid) {
        int hash = methodUid.indexOf('#');
        return hash < 0 ? methodUid : methodUid.substring(0, hash);
    }

    private static boolean isMainSource(Path path) {
        return path.toString().replace('\\', '/').contains("/src/main/java/");
    }

    private void clean(long projectId) {
        try (Session session = driver.session()) {
            session.run("MATCH (n:CodeNode {projectId:$pid}) DETACH DELETE n",
                    Map.of("pid", projectId)).consume();
        }
    }
}
