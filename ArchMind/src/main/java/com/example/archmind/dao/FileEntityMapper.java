package com.example.archmind.dao;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.example.archmind.entity.FileEntity;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

@Mapper
public interface FileEntityMapper extends BaseMapper<FileEntity> {

    /** 按文件数降序的语言分布，用于概况里"语言"的兜底 */
    @Select("""
            SELECT language AS language, COUNT(*) AS cnt
            FROM `file`
            WHERE project_id = #{projectId}
              AND file_type = 'file'
              AND language IS NOT NULL
              AND language <> ''
            GROUP BY language
            ORDER BY cnt DESC, language
            """)
    List<LanguageCount> selectLanguageStats(@Param("projectId") Long projectId);
}
