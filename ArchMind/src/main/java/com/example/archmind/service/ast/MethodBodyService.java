package com.example.archmind.service.ast;

import com.example.archmind.dto.response.MethodBodyResponse;

/**
 * 方法源码切片：从图上拿行号 → 定位磁盘文件 → 按行切出代码。
 *
 * <p>这是「讲解深度」的唯一来源。链路：{@code Method.fileId} → {@code file} 表拿相对路径 →
 * {@code project_source.content}（解压根目录）resolve → 读文本 → 按 {@code startLine/endLine} 切片。</p>
 */
public interface MethodBodyService {

    /**
     * @param uid        起点类全限定名
     * @param methodUid  可选，方法 uid（{@code owner#signature}）。给了就只切这一个方法
     * @param maxMethods 只给类时最多返回几个方法，服务端会再 clamp
     * @param maxLines   切片总行数上限，服务端会再 clamp
     */
    MethodBodyResponse methodBody(Long projectId, String uid, String methodUid,
                                  int maxMethods, int maxLines);
}
