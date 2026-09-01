package com.example.server.experiment;

import com.example.server.utils.OcrUtils;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import javax.imageio.ImageIO;
import java.awt.*;
import java.awt.image.BufferedImage;
import java.io.File;

/**
 * OCR 修复验证测试
 */
@SpringBootTest
public class OcrFixTest {

    @Autowired
    private OcrUtils ocrUtils;

    @Test
    public void testOcrWithChineseText() throws Exception {
        // 创建测试图片
        BufferedImage img = new BufferedImage(800, 200, BufferedImage.TYPE_INT_RGB);
        Graphics2D g = img.createGraphics();
        g.setColor(Color.WHITE);
        g.fillRect(0, 0, 800, 200);
        g.setColor(Color.BLACK);
        g.setFont(new Font("Noto Sans CJK SC", Font.PLAIN, 48));

        String text1 = "进口牛肉配额制";
        String text2 = "268.8万吨";

        FontMetrics fm = g.getFontMetrics();
        int x1 = (800 - fm.stringWidth(text1)) / 2;
        int x2 = (800 - fm.stringWidth(text2)) / 2;

        g.drawString(text1, x1, 80);
        g.drawString(text2, x2, 150);
        g.dispose();

        // 保存为临时文件
        File tempFile = File.createTempFile("ocr-test-", ".png");
        ImageIO.write(img, "png", tempFile);

        System.out.println("=== 测试图片已创建 ===");
        System.out.println("路径: " + tempFile.getAbsolutePath());
        System.out.println();

        // 调用 OCR
        System.out.println("=== 调用 OCR ===");
        String result = ocrUtils.recognize(tempFile);

        System.out.println("=== OCR 结果 ===");
        System.out.println(result);
        System.out.println();

        // 验证结果
        if (result.contains("进口牛肉配额制")) {
            System.out.println("✅ OCR 测试通过！");
            System.out.println("✅ 成功识别中文文本");
            if (result.contains("Error") || result.contains("Estimating")) {
                System.out.println("❌ 但仍然包含错误信息！");
            } else {
                System.out.println("✅ 没有混入 stderr 日志");
            }
        } else {
            System.out.println("❌ OCR 测试失败！");
            System.out.println("期望包含: 进口牛肉配额制");
            System.out.println("实际结果: " + result);
        }

        // 清理
        tempFile.delete();
    }
}
