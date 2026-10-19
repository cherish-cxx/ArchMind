package com.example.archmind.dto.response;

import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * P5 的防漏测试：Python 发来的 {@code graph} 必须真的被 Java DTO 接住。
 *
 * <p>这条测试存在的唯一理由：Spring Boot 默认忽略未知字段，Java DTO 少写一个字段
 * <b>不会报错、只会静默丢</b>。只测 setter 发现不了，必须走一次真实反序列化。</p>
 */
class AgentResponseGraphTest {

    private final ObjectMapper mapper = new ObjectMapper();

    @Test
    void graphIsDeserializedAndNotSilentlyDropped() {
        String json = """
                {
                  "conversationId": "c1",
                  "message": "订单创建从 OrderController 开始",
                  "sessionExpired": false,
                  "citations": [],
                  "uiCommands": [],
                  "suggestions": [],
                  "steps": [],
                  "meta": {
                    "intent": "EXPLORE_FLOW",
                    "confidence": 0.95,
                    "graphRev": 1,
                    "latencyMs": 42
                  },
                  "graph": {
                    "nodes": [
                      {
                        "uid": "com.demo.OrderController",
                        "name": "OrderController",
                        "kind": "CLASS",
                        "packageName": "com.demo",
                        "module": "demo",
                        "depth": 0,
                        "viaFanout": false,
                        "edgeCount": 1
                      }
                    ],
                    "edges": [
                      {
                        "from": "com.demo.OrderController",
                        "to": "com.demo.OrderService",
                        "rel": "CALLS",
                        "callCount": 2,
                        "viaFanout": false
                      }
                    ],
                    "gaps": [
                      {
                        "at": "com.demo.OrderMapper",
                        "reason": "NO_DOWNSTREAM_EDGE"
                      }
                    ],
                    "truncated": true
                  }
                }
                """;

        AgentResponse response = mapper.readValue(json, AgentResponse.class);

        assertNotNull(response.getGraph(), "graph 字段被静默丢了");
        assertEquals(1, response.getGraph().getNodes().size());
        assertEquals(1, response.getGraph().getEdges().size());
        assertEquals(1, response.getGraph().getGaps().size());

        AgentResponse.GraphNode node = response.getGraph().getNodes().get(0);
        assertEquals("com.demo.OrderController", node.getUid());
        assertEquals("CLASS", node.getKind());
        assertEquals("com.demo", node.getPackageName());
        assertEquals(0, node.getDepth());
        assertEquals(1, node.getEdgeCount());

        AgentResponse.GraphEdge edge = response.getGraph().getEdges().get(0);
        assertEquals("com.demo.OrderController", edge.getFrom());
        assertEquals("com.demo.OrderService", edge.getTo());
        assertEquals("CALLS", edge.getRel());
        assertEquals(2, edge.getCallCount());

        assertEquals("NO_DOWNSTREAM_EDGE", response.getGraph().getGaps().get(0).getReason());
        assertTrue(response.getGraph().isTruncated());
    }
}
