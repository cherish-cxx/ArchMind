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

//    定义静态cypher语句
    private static final String READ_REV =
            "MATCH (g:GraphMeta {projectId: $pid}) RETURN g.rev AS rev";

    private final Driver driver;

    /**
     *
     * @param projectId
     * session是Neo4j java驱动里的数据库会话
     * 通过 driver Neo4j连接驱动
     * driver.session想总管申请开一个会话通道
     * @return 版本号
     */
    @Override
    public long currentRev(Long projectId) {
        if (projectId == null) {
            return 0L;
        }
        try (Session session = driver.session()) {
//            这个session会话发送Cypher查询语句，带上pid参数，让Neo4j执行查询
            Result result = session.run(READ_REV, Map.of("pid", projectId));
//            没查到返回0L
            if (!result.hasNext()) {
                return 0L;
            }
//读取这一行查询结果，去除这一行里面，交rev的字段的值 并且包装成一个value对象
            Value rev = result.next().get("rev");
//            节点存在但是还是需要判断节点是否为空
            return rev == null || rev.isNull() ? 0L : rev.asLong();
        } catch (Exception e) {
            log.warn("读取图版本号失败，按 0 处理 projectId={}", projectId, e);
            return 0L;
        }
    }
}
