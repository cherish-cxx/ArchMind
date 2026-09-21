package com.example.archmind.service.impl;

import com.example.archmind.service.ast.GraphRevService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.neo4j.driver.Driver;
import org.neo4j.driver.Result;
import org.neo4j.driver.Session;
import org.neo4j.driver.Value;
import org.springframework.stereotype.Service;

import java.util.Map;

@Service
@RequiredArgsConstructor
@Slf4j
public class GraphRevServiceImpl implements GraphRevService {

    private static final String READ_REV =
            "MATCH (g:GraphMeta {projectId: $pid}) RETURN g.rev AS rev";

    private final Driver driver;

    @Override
    public long currentRev(Long projectId) {
        if (projectId == null) {
            return 0L;
        }
        try (Session session = driver.session()) {
            Result result = session.run(READ_REV, Map.of("pid", projectId));
            if (!result.hasNext()) {
                return 0L;
            }
            Value rev = result.next().get("rev");
            return rev == null || rev.isNull() ? 0L : rev.asLong();
        } catch (Exception e) {
            log.warn("读取图版本号失败，按 0 处理 projectId={}", projectId, e);
            return 0L;
        }
    }
}
