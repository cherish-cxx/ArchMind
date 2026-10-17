package com.example.archmind.service.agent;

/**
 * 没能拿到一个合法响应：超时 / 连不上 / Python 侧 5xx。
 *
 * <p>属于「对面暂时不行」，给用户一句诚实的话，**不 500**。
 * 与 Python 侧 {@code ToolTransportError} 是同一个语义。</p>
 */
public class AgentTransportException extends AgentClientException {

    public AgentTransportException(String message) {
        super(message);
    }

    public AgentTransportException(String message, Throwable cause) {
        super(message, cause);
    }
}
