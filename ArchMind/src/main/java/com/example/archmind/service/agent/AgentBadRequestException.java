package com.example.archmind.service.agent;

/**
 * Python 侧返回 <b>4xx（401 除外）</b>，例如 FastAPI 的 422 校验失败。
 *
 * <p>网关把请求体基本原样透传，所以这多半是**用户传了非法参数**
 * （比如 navigate 的 {@code op} 填了个枚举外的值），也可能是 Java 的
 * DTO 字段名与 Python 的模型漂移了。两种情况都该给用户一句
 * 「参数有误」，而不是「服务不可用」—— 后者会让用户白等重试。</p>
 */
public class AgentBadRequestException extends AgentClientException {

    public AgentBadRequestException(String message) {
        super(message);
    }
}
