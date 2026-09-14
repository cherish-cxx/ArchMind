package com.example.archmind.dto.response;

import lombok.Data;

import java.util.List;

/**
 * 一条「类级关联」：目标类 + 关系类型 + 证据说明。
 * 证据是给人看的短句，例如 "handleLogin(String,String) → login(String,String)"（调用）
 * 或 "field" / "signature"（依赖来源）。
 */
@Data
public class RelatedClassDTO {

    private ClassNodeDTO clazz;

    /** CALLS / EXTENDS / IMPLEMENTS / DEPENDS */
    private String relationType;

    private List<String> evidence;
}
