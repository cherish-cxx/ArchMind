package com.example.archmind.service.domain;

/**
 * 业务领域：uid 全项目内唯一，kind 为 business / infrastructure。
 * 业务领域 uid/name 取包名里的业务 key；基础设施领域 uid 固定为 infrastructure。
 */
public record BusinessArea(String uid, String name, String kind) {
}
