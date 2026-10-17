package com.example.archmind.common.util;


import io.jsonwebtoken.*;
import io.jsonwebtoken.security.Keys;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.util.Date;
import java.util.HashMap;
import java.util.Map;

@Slf4j
@Component
public class JwtUtil {

    @Value("${jwt.secret}")
    private String secret;

    @Value("${jwt.access-token-expire}")
    private Long accessTokenExpire;

    @Value("${jwt.refresh-token-expire}")
    private Long refreshTokenExpire;

    @Value("${jwt.issuer}")
    private String issuer;

//    获取签名密钥，也就是基于UTF-8编码密钥字节数组生成的算法密钥
    private SecretKey getSecretKey(){
        return Keys.hmacShaKeyFor(secret.getBytes(StandardCharsets.UTF_8));
    }

//    生成token令牌
    public String generateAccessToken(Long userId,String username){
        Map<String, Object> claims = new HashMap<>();
        claims.put("userId", userId);
        claims.put("username", username);
        claims.put("type", "access");

        return generateToken(claims, accessTokenExpire);
    }

//    生成refreshToken
public String generateRefreshToken(Long userId, String username) {
    Map<String, Object> claims = new HashMap<>();
    claims.put("userId", userId);
    claims.put("username", username);
    claims.put("type", "refresh");

    return generateToken(claims, refreshTokenExpire);
    }

    private String generateToken(Map<String, Object> claims, Long expireSeconds) {
        Date now = new Date();
        Date expireDate = new Date(now.getTime() + expireSeconds * 1000);

        return Jwts.builder()
                .setClaims(claims)
                .setIssuer(issuer)
                .setIssuedAt(now)
                .setExpiration(expireDate)
                .signWith(getSecretKey(), SignatureAlgorithm.HS256)
                .compact();
    }
//此处做的是校验，拦截请求的落点在JwtAuthenticationFilter
    public Claims parseToken(String token) {
        try {
            return Jwts.parser()
                    .verifyWith(getSecretKey()) //签名是否正确
                    .requireIssuer(issuer)  //签发者是否正确
                    .build()
                    //是否过期
                    .parseSignedClaims(token)
                    .getPayload();
        } catch (ExpiredJwtException e) {
            log.warn("Token 已过期: {}", e.getMessage());
            throw new JwtException("Token 已过期");
        } catch (JwtException e) {
            log.warn("Token 无效: {}", e.getMessage());
            throw new JwtException("Token 无效");
        }
    }

    public Long getUserIdFromToken(String token) {
        Claims claims = parseToken(token);
        return claims.get("userId", Long.class);
    }

    /**
     * 从 Token 中获取用户名
     */
    public String getUsernameFromToken(String token) {
        Claims claims = parseToken(token);
        return claims.get("username", String.class);
    }

    /**
     * 获取 Token 类型（access/refresh）
     */
    public String getTokenType(String token) {
        Claims claims = parseToken(token);
        return claims.get("type", String.class);
    }

    /**
     * 校验 Token 是否有效
     */
    public boolean validateToken(String token) {
        try {
            parseToken(token);
            return true;
        } catch (JwtException e) {
            return false;
        }
    }

    /**
     * 获取 Token 过期时间
     */
    public Date getExpirationDate(String token) {
        Claims claims = parseToken(token);
        return claims.getExpiration();
    }

    /**
     * 获取 Token 剩余有效时间（秒）
     */
    public Long getRemainingTime(String token) {
        Date expiration = getExpirationDate(token);
        long now = System.currentTimeMillis();
        long remaining = expiration.getTime() - now;
        return remaining > 0 ? remaining / 1000 : 0;
    }
}
