package com.example.archmind.dto.response;

import lombok.Data;

import java.util.List;

/**
 * 按 uid 游标分页的类清单，供 Python 建向量索引时流式拉取。
 *
 * <p>{@code nextCursor} 为 null 表示已经拉完；每页都带 {@code rev}，
 * 调用方发现 rev 变化时必须中止重来，避免拉到两次分析的混合快照。</p>
 */
@Data
public class ClassPageResponse {

    private long rev;

    private List<ClassNodeDTO> items = List.of();

    /** 下一页的 afterUid；null = 没有下一页 */
    private String nextCursor;
}
