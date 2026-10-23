package com.example.archmind.service;

import com.example.archmind.dto.response.ClassSourceResponse;

/**
 * 类源码切片：从图上拿类的文件与行区间 → 从磁盘切出源码，供前端代码抽屉展示。
 */
public interface ClassSourceService {

    /**
     * @param uid      类全限定名
     * @param maxLines 返回的最大行数，服务端会再 clamp
     */
    ClassSourceResponse classSource(Long projectId, String uid, int maxLines);
}
