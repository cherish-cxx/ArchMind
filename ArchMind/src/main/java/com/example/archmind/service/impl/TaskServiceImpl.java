package com.example.archmind.service.impl;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import com.example.archmind.common.constant.AnalysisStage;
import com.example.archmind.common.constant.ProjectAnalysisStatus;
import com.example.archmind.common.constant.RedisKeys;
import com.example.archmind.common.constant.TaskStatus;
import com.example.archmind.common.constant.TaskType;
import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.common.util.RedisUtil;
import com.example.archmind.dao.ProjectMapper;
import com.example.archmind.dao.TaskMapper;
import com.example.archmind.dto.response.ProjectOverviewResponse;
import com.example.archmind.dto.response.TaskResponse;
import com.example.archmind.entity.Project;
import com.example.archmind.entity.Task;
import com.example.archmind.service.ProjectOverviewService;
import com.example.archmind.service.TaskService;
import com.example.archmind.service.task.AnalysisTaskExecutor;
import jakarta.annotation.PostConstruct;
import lombok.extern.slf4j.Slf4j;
import org.springframework.context.annotation.Lazy;
import org.springframework.stereotype.Service;
import tools.jackson.core.JacksonException;
import tools.jackson.core.type.TypeReference;
import tools.jackson.databind.ObjectMapper;

import java.time.LocalDateTime;
import java.util.List;
import java.util.concurrent.TimeUnit;

/**
 * 任务状态机：唯一往 task 表写状态的地方。
 * 提交/查询由 Controller 调；状态转移由 AnalysisTaskExecutor 驱动。
 */
@Service
@Slf4j
public class TaskServiceImpl implements TaskService {

    /** 项目级分析锁：防止同一项目并发提交导致重复调用 LLM */
    public static final String ANALYZE_LOCK_KEY_PREFIX = RedisKeys.ANALYZE_LOCK;
    /** 任务实时进度（高频，只写 Redis） */
    public static final String TASK_PROGRESS_KEY_PREFIX = RedisKeys.TASK_PROGRESS;

    private static final long LOCK_TTL_SECONDS = 300;
    private static final int ERROR_MAX_LEN = 500;

    private final ProjectOverviewService projectOverviewService;
    private final RedisUtil redisUtil;
    private final TaskMapper taskMapper;
    private final ProjectMapper projectMapper;
    private final ObjectMapper objectMapper;
    private final AnalysisTaskExecutor executor;

    /**
     * 显式构造函数 + @Lazy：
     * TaskServiceImpl → AnalysisTaskExecutor → TaskService(TaskServiceImpl) 构成循环依赖，
     * Spring Boot 默认禁止循环引用，用 @Lazy 让执行器延迟到真正调用时才创建，打破环。
     */
    public TaskServiceImpl(ProjectOverviewService projectOverviewService,
                           RedisUtil redisUtil,
                           TaskMapper taskMapper,
                           ProjectMapper projectMapper,
                           ObjectMapper objectMapper,
                           @Lazy AnalysisTaskExecutor executor) {
        this.projectOverviewService = projectOverviewService;
        this.redisUtil = redisUtil;
        this.taskMapper = taskMapper;
        this.projectMapper = projectMapper;
        this.objectMapper = objectMapper;
        this.executor = executor;
    }

    /**
     * 启动兜底：应用重启后，内存队列里的任务已丢失，把遗留的 PENDING/RUNNING 标成 FAILED，避免僵尸卡住
     * 这里是一个隐式调用，调用者不是我，而是IOC容器
     * @PostConstruct 是Sping的生命周期注解，语义是：容器吧这个bean的依赖注入完成后，对外提供服务之前，自动调用它一次
     * */
    @PostConstruct
    public void resetStaleTasks() {
        try {
            List<Task> stale = taskMapper.selectList(new LambdaQueryWrapper<Task>()
                    .in(Task::getStatus, TaskStatus.PENDING, TaskStatus.RUNNING));
            int updated = taskMapper.update(null, new LambdaUpdateWrapper<Task>()
                    .in(Task::getStatus, TaskStatus.PENDING, TaskStatus.RUNNING)
                    .set(Task::getStatus, TaskStatus.FAILED)
                    .set(Task::getError, "应用重启，任务中断")
                    .set(Task::getUpdateTime, LocalDateTime.now()));
            // 分析任务中断后项目状态不能停在 PENDING，否则描述补齐的闸门会一直挡住
            for (Task task : stale) {
                if (TaskType.ANALYSIS.equals(task.getType()) && task.getProjectId() != null) {
                    Project update = new Project();
                    update.setId(task.getProjectId());
                    update.setAnalysisStatus(ProjectAnalysisStatus.FAILED);
                    projectMapper.updateById(update);
                }
            }
            if (updated > 0) {
                log.warn("启动清理僵尸任务 {} 条", updated);
            }
        } catch (Exception e) {
            log.warn("启动清理僵尸任务失败（task 表可能尚未创建）", e);
        }
    }

    // ==================== 提交分析任务 ====================

    /** submitAnalysis这里是被项目分析接口调用
     * 参数
     * Long projectId, Long userId, boolean force
     * 实现逻辑：
     * 对已有概括的项目，不创建任务，直接合成已完成的响应，直接到数据库拿
     * 对于没有概括的新项目，先进行上锁，避免多个请求，并将该项目信息以及目前分析状态入库
     * 核心：开启异步分析，并同时return task。避免同步超时
     *
     */
    @Override
    public TaskResponse submitAnalysis(Long projectId, Long userId, boolean force) {
        // 快路径：非强刷且已有概况 → 不建任务、不占线程，直接合成"已完成"响应(已经完成分析的就会走这一条路)
        if (!force) {
            ProjectOverviewResponse existing = projectOverviewService.tryGetExistingOverview(projectId);
            if (existing != null) {
                return TaskResponse.builder()
                        .projectId(projectId)
                        .type(TaskType.ANALYSIS)
                        .status(TaskStatus.SUCCESS)
                        .progress(100)
                        .detail(existing)
                        .build();
            }
        }

        // 没有进行过分析的就走这里，先上锁，避免多个请求，然后创建任务记录到Task中
        // 抢锁：SETNX 原子操作，同一项目同一时刻只有一个请求能通过
        String lockKey = ANALYZE_LOCK_KEY_PREFIX + projectId;
        Boolean got = redisUtil.setIfAbsent(lockKey, "1", LOCK_TTL_SECONDS, TimeUnit.SECONDS);
        if (!Boolean.TRUE.equals(got)) {
            throw new BusinessException("该项目正在分析中，请稍后再试");
        }

        // 先落库（不套大事务），再投递
        Task task = new Task();
        task.setProjectId(projectId);
        task.setUserId(userId);
        task.setType(TaskType.ANALYSIS);
        task.setStatus(TaskStatus.PENDING);
        task.setProgress(0);
        task.setCreateTime(LocalDateTime.now());
        task.setUpdateTime(LocalDateTime.now());

        try {
            taskMapper.insert(task);
        } catch (Exception e) {
            redisUtil.delete(lockKey);   // 落库失败要放锁，否则该项目被永久锁死
            throw new BusinessException("创建分析任务失败: " + e.getMessage());
        }
        // 重分析期间状态不能停留在 COMPLETED，否则描述补齐的闸门会误放行
        updateProjectStatus(task.getId(), ProjectAnalysisStatus.PENDING);
//其实是被AOP代理拦去线程池，如果去掉注解就变成了同步跑了
        executor.run(task.getId(), projectId, userId);
        return toResponse(task);
    }

    // ==================== 查询 ====================

    @Override
    public TaskResponse get(Long taskId, Long currentUserId) {
        Task task = taskMapper.selectById(taskId);
        if (task == null) {
            throw new BusinessException("任务不存在: " + taskId);
        }
        checkOwner(task, currentUserId);
        overlayLiveProgress(task);
        return toResponse(task);
    }

    @Override
    public TaskResponse getLatestByProject(Long projectId, Long currentUserId) {
        // 只看分析任务：描述补齐任务也在 task 表里，但它不是「项目是否在分析」的依据
        Task task = taskMapper.selectOne(new LambdaQueryWrapper<Task>()
                .eq(Task::getProjectId, projectId)
                .eq(Task::getType, TaskType.ANALYSIS)
                .orderByDesc(Task::getId)
                .last("limit 1"));
        if (task == null) {
            return null;
        }
        checkOwner(task, currentUserId);
        overlayLiveProgress(task);
        return toResponse(task);
    }

    // ==================== 状态机 ====================

    @Override
    public void markRunning(Long taskId) {
        Task upd = new Task();
        upd.setId(taskId);
        upd.setStatus(TaskStatus.RUNNING);
        upd.setUpdateTime(LocalDateTime.now());
        taskMapper.updateById(upd);
    }

//    更新task的信息，
    @Override
    public void enterStage(Long taskId, Long projectId, AnalysisStage stage) {
        Task upd = new Task();
        upd.setId(taskId);
        upd.setCurrentStage(stage.name());
        upd.setProgress(stage.getStart());
        upd.setUpdateTime(LocalDateTime.now());
        taskMapper.updateById(upd);

        // 阶段起始也同步给 Redis，避免刚 enterStage、还没 report 时查询读到上一阶段的旧进度
        redisUtil.set(TASK_PROGRESS_KEY_PREFIX + taskId,
                stage.getStart() + "|" + stage.name(), 1, TimeUnit.HOURS);
        renewAnalyzeLock(projectId);
    }

    @Override
    public void reportProgress(Long taskId, Long projectId, AnalysisStage stage, int pctWithin) {
        int progress = stage.progressAt(pctWithin);
        redisUtil.set(TASK_PROGRESS_KEY_PREFIX + taskId,
                progress + "|" + stage.name(), 1, TimeUnit.HOURS);
        // 大项目分析可能超过锁的 TTL，不续期会让描述补齐的闸门误判「当前没有分析」
        renewAnalyzeLock(projectId);
    }

    private void renewAnalyzeLock(Long projectId) {
        if (projectId == null) {
            return;
        }
        try {
            redisUtil.expire(ANALYZE_LOCK_KEY_PREFIX + projectId, LOCK_TTL_SECONDS, TimeUnit.SECONDS);
        } catch (Exception e) {
            log.debug("续期分析锁失败 projectId={}", projectId, e);
        }
    }

    @Override
    public void markSuccess(Long taskId, ProjectOverviewResponse overview) {
        Task upd = new Task();
        upd.setId(taskId);
        upd.setStatus(TaskStatus.SUCCESS);
        upd.setProgress(100);
        upd.setDetail(toJsonQuietly(overview));
        upd.setUpdateTime(LocalDateTime.now());
        taskMapper.updateById(upd);

        updateProjectStatus(taskId, ProjectAnalysisStatus.COMPLETED);
        redisUtil.delete(TASK_PROGRESS_KEY_PREFIX + taskId);
    }

    @Override
    public void markFailed(Long taskId, String error) {
        Task upd = new Task();
        upd.setId(taskId);
        upd.setStatus(TaskStatus.FAILED);
        upd.setError(truncate(error));
        upd.setUpdateTime(LocalDateTime.now());
        taskMapper.updateById(upd);

        updateProjectStatus(taskId, ProjectAnalysisStatus.FAILED);
        redisUtil.delete(TASK_PROGRESS_KEY_PREFIX + taskId);
    }

    @Override
    public void releaseLock(Long projectId) {
        redisUtil.delete(ANALYZE_LOCK_KEY_PREFIX + projectId);
    }

    // ==================== 内部工具 ====================

    /** 非终态时，用 Redis 里的实时进度覆盖 DB 的旧值 */
    private void overlayLiveProgress(Task task) {
        if (TaskStatus.isTerminal(task.getStatus())) {
            return;
        }
        Object v = redisUtil.get(TASK_PROGRESS_KEY_PREFIX + task.getId());
        if (!(v instanceof String s)) {
            return;
        }
        int idx = s.indexOf('|');
        if (idx <= 0) {
            return;
        }
        try {
            task.setProgress(Integer.parseInt(s.substring(0, idx)));
            task.setCurrentStage(s.substring(idx + 1));
        } catch (NumberFormatException ignored) {
            // 格式异常就保留 DB 里的值
        }
    }

    private void checkOwner(Task task, Long currentUserId) {
        if (currentUserId != null && task.getUserId() != null && !currentUserId.equals(task.getUserId())) {
            throw new BusinessException("无权查看该任务");
        }
    }

    private void updateProjectStatus(Long taskId, String status) {
        try {
            Task task = taskMapper.selectById(taskId);
            if (task == null || task.getProjectId() == null) {
                return;
            }
            Project update = new Project();
            update.setId(task.getProjectId());
            update.setAnalysisStatus(status);
            projectMapper.updateById(update);
        } catch (Exception e) {
            log.warn("更新项目分析状态失败 taskId={}, status={}", taskId, status, e);
        }
    }

    private TaskResponse toResponse(Task task) {
        return TaskResponse.builder()
                .taskId(task.getId())
                .projectId(task.getProjectId())
                .type(task.getType())
                .status(task.getStatus())
                .progress(task.getProgress())
                .currentStage(task.getCurrentStage())
                .stageLabel(stageLabel(task.getCurrentStage()))
                .detail(parseDetail(task.getDetail()))
                .error(task.getError())
                .createTime(task.getCreateTime())
                .updateTime(task.getUpdateTime())
                .build();
    }

    private String stageLabel(String stageName) {
        if (stageName == null) {
            return null;
        }
        try {
            return AnalysisStage.valueOf(stageName).getLabel();
        } catch (IllegalArgumentException e) {
            return null;
        }
    }

    private Object parseDetail(String detail) {
        if (detail == null || detail.isBlank()) {
            return null;
        }
        try {
            return objectMapper.readValue(detail, new TypeReference<Object>() {});
        } catch (JacksonException e) {
            log.warn("解析任务 detail 失败", e);
            return null;
        }
    }

    private String toJsonQuietly(Object value) {
        if (value == null) {
            return null;
        }
        try {
            return objectMapper.writeValueAsString(value);
        } catch (JacksonException e) {
            log.warn("序列化任务 detail 失败", e);
            return null;
        }
    }

    private String truncate(String error) {
        if (error == null) {
            return "未知错误";
        }
        return error.length() <= ERROR_MAX_LEN ? error : error.substring(0, ERROR_MAX_LEN);
    }
}
