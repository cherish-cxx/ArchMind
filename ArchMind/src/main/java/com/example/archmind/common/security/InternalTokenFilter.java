package com.example.archmind.common.security;

import com.example.archmind.common.result.Result;
import com.example.archmind.common.result.ResultCode;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import lombok.extern.slf4j.Slf4j;
import org.springframework.web.filter.OncePerRequestFilter;
import tools.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * {@code /internal/**} 的门锁：校验请求头 {@code X-Internal-Token} 共享密钥。
 *
 * <p>只拦 {@code /internal/**}，其余路径直接放行。**不走 JWT** —— 调用方是 Agent 服务，
 * 按设计它永远拿不到用户 token；两边只共享一个密钥。</p>
 *
 * <p>非 {@code @Component}：由 SecurityConfig 显式建成 Bean 并挂进过滤器链，
 * 避免被 Spring Boot 当作普通 Servlet Filter 再注册一次。</p>
 */
@Slf4j
public class InternalTokenFilter extends OncePerRequestFilter {

    private static final String HEADER = "X-Internal-Token";
    private static final String PREFIX = "/internal/";

    private final byte[] expectedToken;
    private final ObjectMapper objectMapper;

    public InternalTokenFilter(String token, ObjectMapper objectMapper) {
        this.expectedToken = token.getBytes(StandardCharsets.UTF_8);
        this.objectMapper = objectMapper;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request,
                                    HttpServletResponse response,
                                    FilterChain filterChain) throws ServletException, IOException {
        if (!request.getRequestURI().startsWith(PREFIX)) {
            filterChain.doFilter(request, response);
            return;
        }

        String token = request.getHeader(HEADER);
        // 定长比较，避免逐字符短路带来的时序侧信道
        if (token != null
                && MessageDigest.isEqual(expectedToken, token.getBytes(StandardCharsets.UTF_8))) {
            filterChain.doFilter(request, response);
            return;
        }

        log.warn("内部接口鉴权失败: {} {}", request.getMethod(), request.getRequestURI());
        response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
        response.setContentType("application/json;charset=UTF-8");
        objectMapper.writeValue(response.getOutputStream(),
                Result.fail(ResultCode.UNAUTHORIZED.getCode(), "内部接口鉴权失败"));
    }
}
