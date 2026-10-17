package com.example.archmind.dto;

import lombok.Data;

/**
 * 屏幕上一条可见的关系，前端上报 viewport 时用。
 *
 * <p>字段名 {@code from} 与 Python 侧 {@code RelationRef} 的别名对齐
 * （那边因为 {@code from} 撞 Python 关键字而显式写了 {@code Field(alias="from")}，
 * Java 这边不是关键字，直接叫 {@code from} 即可）。</p>
 */
@Data
public class RelationRefDTO {
    private String from;
    private String to;
    private String rel;
}
