package com.example.archmind.service.agent;

/**
 * 调用 Python agent-service 时抛出的所有异常的基类。
 *
 * <p>与 Python 侧 {@code app/tools/java_client.py} 的 {@code JavaClientError} 对称 ——
 * 那条链路是 Python 调 Java，这条是 Java 调 Python，两边的错误分类必须一样细，
 * 否则排查时「对面挂了」和「我配错了」会混在一起。</p>
 */
public class AgentClientException extends RuntimeException {

    public AgentClientException(String message) {
        super(message);
    }

    public AgentClientException(String message, Throwable cause) {
        super(message, cause);
    }
}
