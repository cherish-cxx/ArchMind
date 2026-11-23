package com.example.archmind.model.ast;

/**
 * 一个外部可触发的入口：HTTP 路由，或定时 / MQ / 事件 / 启动任务。
 *
 * <p>uid 规则：HTTP 为「动词 + 空格 + 路径」（如 "POST /orders"）；
 * 非 HTTP 为「大写 KIND + 空格 + handlerUid」（如 "SCHEDULED demo.Job#run()"）。
 *
 * @param uid        入口唯一键
 * @param kind       http / scheduled / mq / event / manual
 * @param httpMethod HTTP 动词；非 HTTP 为 null
 * @param path       归一化后的路径；非 HTTP 为 null
 * @param handlerUid 处理方法的 uid，格式 ownerQualifiedName + "#" + signature
 * @param inferred   路径是否由方法名推断（方法级映射未写路径时为 true）
 */
public record ParsedEndpoint(String uid, String kind, String httpMethod, String path,
                             String handlerUid, boolean inferred) {
}
