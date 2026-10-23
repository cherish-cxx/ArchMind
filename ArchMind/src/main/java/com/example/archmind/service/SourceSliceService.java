package com.example.archmind.service;

import java.nio.file.Path;

/**
 * 项目源码读取：定位解压根目录，并在安全边界内按相对路径读取文本。
 *
 * <p>供方法切片（{@code MethodBodyService}）与类源码（{@code ClassSourceService}）复用，
 * 统一承接路径穿越防护与文件大小/二进制限制。</p>
 */
public interface SourceSliceService {

    /** 解析项目最新的解压根目录（project_source.content），不存在则抛业务异常。 */
    Path resolveExtractDir(Long projectId);

    /** 读取解压根目录下某个相对路径文件的全部行；路径越界、二进制或超限时抛业务异常。 */
    String[] readLines(Path extractDir, String relativePath);
}
