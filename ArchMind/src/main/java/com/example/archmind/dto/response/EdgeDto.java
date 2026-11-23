package com.example.archmind.dto.response;

/**
 * 能力内部的类级调用边：fromUid 与 toUid 都是能力成员类的 uid。
 * 方法级 CALLS 收窄到成员之间后按类去重，供链路图绘制。
 */
public record EdgeDto(String fromUid, String toUid) {
}
