package com.example.archmind.service.agent;

/**
 * Python 侧返回 <b>401</b> —— 两边 {@code INTERNAL_TOKEN} 对不上。
 *
 * <p><b>这是配置错误，不是业务结果</b>，必须炸得响亮（ERROR 日志 + 500），
 * 绝不能被当成「对话服务暂时不可用」静默降级 —— 那会让一个部署事故
 * 看起来像一次网络抖动。</p>
 *
 * <p>与 Python 侧 {@code InternalAuthError} 是同一个语义。</p>
 */
public class AgentAuthException extends AgentClientException {

    public AgentAuthException(String message) {
        super(message);
    }
}
