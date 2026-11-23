package com.example.archmind.dto.response;

/**
 * 能力成员类：depth 为从入口出发的最短跳数（入口类为 0）。
 */
public record MemberDto(String uid, String name, int depth, String role) {
}
