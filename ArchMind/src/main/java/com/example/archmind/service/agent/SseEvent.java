package com.example.archmind.service.agent;

/**
 * SSE 的一帧：事件名 + 原始 data 文本（通常是 JSON）。
 *
 * <p>保留成字符串而不是直接反序列化，是因为网关对 <b>step</b> 事件只是转发，
 * 只有 <b>answer</b> 需要解析后做弱校验 —— 解析一次就够了，不必每帧都建对象。</p>
 */
public record SseEvent(String name, String data) {
}
