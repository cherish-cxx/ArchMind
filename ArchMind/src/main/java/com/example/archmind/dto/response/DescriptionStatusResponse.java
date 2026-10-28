package com.example.archmind.dto.response;

import lombok.Data;

/**
 * 项目描述覆盖情况。
 *
 * <p>进度分母用 {@code describable}（排除内部类等不参与描述的节点），
 * 与选批条件保持一致，避免 remaining 永远落不到 0。</p>
 */
@Data
public class DescriptionStatusResponse {

    private Long projectId;

    private String analysisStatus;

    private int total;

    private int describable;

    /** describable - pending */
    private int described;

    /** 仍未生成描述的类数 */
    private int pending;

    private long rev;
}
