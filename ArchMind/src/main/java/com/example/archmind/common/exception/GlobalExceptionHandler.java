package com.example.archmind.common.exception;

import com.example.archmind.common.result.Result;
import com.example.archmind.common.result.ResultCode;
import com.example.archmind.service.agent.AgentAuthException;
import com.example.archmind.service.agent.AgentBadRequestException;
import com.example.archmind.service.agent.AgentTransportException;
import jakarta.validation.ConstraintViolation;
import jakarta.validation.ConstraintViolationException;
import lombok.extern.slf4j.Slf4j;
import org.springframework.validation.BindException;
import org.springframework.validation.FieldError;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.util.stream.Collectors;

@RestControllerAdvice
@Slf4j
public class GlobalExceptionHandler{

        /**
         * 处理业务异常
         */
        @ExceptionHandler(BusinessException.class)
        public Result<Void> handleBusinessException(BusinessException e) {
            log.warn("业务异常: {}", e.getMessage());
            return Result.fail(e.getCode(), e.getMessage());
        }

        /**
         * Agent 内部鉴权失败（两边 INTERNAL_TOKEN 对不上）。
         *
         * <p><b>这是配置错误，必须响</b>：ERROR 级别日志 + 500，绝不降级成
         * 「服务不可用」—— 那会让一次部署事故看起来像一次网络抖动。</p>
         */
        @ExceptionHandler(AgentAuthException.class)
        public Result<Void> handleAgentAuthException(AgentAuthException e) {
            log.error("Agent 内部鉴权失败 —— 查两边 INTERNAL_TOKEN 是否一致: {}", e.getMessage());
            return Result.fail(ResultCode.FAIL.getCode(), "系统内部错误，请稍后重试");
        }

        /**
         * Agent 拒绝了请求（4xx，如参数非法或 DTO 契约漂移）。
         *
         * <p>给用户「参数有误」而不是「服务不可用」—— 后者会让用户白等重试。</p>
         */
        @ExceptionHandler(AgentBadRequestException.class)
        public Result<Void> handleAgentBadRequestException(AgentBadRequestException e) {
            log.warn("Agent 拒绝请求: {}", e.getMessage());
            return Result.fail(ResultCode.PARAM_ERROR.getCode(), "对话请求参数有误，请检查后重试");
        }

        /**
         * Agent 服务不可达（超时 / 连不上 / 5xx）。
         *
         * <p>诚实说一句，**不 500** —— 和 Python 侧工具挂掉时降级是同一个原则。</p>
         */
        @ExceptionHandler(AgentTransportException.class)
        public Result<Void> handleAgentTransportException(AgentTransportException e) {
            log.warn("Agent 服务不可达: {}", e.getMessage());
            return Result.fail(ResultCode.FAIL.getCode(), "对话服务暂时不可用，请稍后再试");
        }

        /**
         * 处理 @Valid 校验异常（RequestBody）
         */
        @ExceptionHandler(MethodArgumentNotValidException.class)
        public Result<Void> handleValidationException(MethodArgumentNotValidException e) {
            String message = e.getBindingResult().getFieldErrors().stream()
                    .map(FieldError::getDefaultMessage)
                    .collect(Collectors.joining("; "));
            log.warn("参数校验失败: {}", message);
            return Result.fail(ResultCode.PARAM_ERROR.getCode(), message);
        }

        /**
         * 处理 @Validated 校验异常（参数绑定）
         */
        @ExceptionHandler(BindException.class)
        public Result<Void> handleBindException(BindException e) {
            String message = e.getBindingResult().getFieldErrors().stream()
                    .map(FieldError::getDefaultMessage)
                    .collect(Collectors.joining("; "));
            log.warn("参数绑定失败: {}", message);
            return Result.fail(ResultCode.PARAM_ERROR.getCode(), message);
        }

        /**
         * 处理约束校验异常
         */
        @ExceptionHandler(ConstraintViolationException.class)
        public Result<Void> handleConstraintViolationException(ConstraintViolationException e) {
            String message = e.getConstraintViolations().stream()
                    .map(ConstraintViolation::getMessage)
                    .collect(Collectors.joining("; "));
            log.warn("约束校验失败: {}", message);
            return Result.fail(ResultCode.PARAM_ERROR.getCode(), message);
        }

        /**
         * 处理其他未捕获异常
         */
        @ExceptionHandler(Exception.class)
        public Result<Void> handleException(Exception e) {
            log.error("系统异常: ", e);
            return Result.fail(ResultCode.FAIL.getCode(), "系统内部错误，请稍后重试");
        }
}
