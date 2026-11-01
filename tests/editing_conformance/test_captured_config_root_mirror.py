"""Lossless c271 provider history: config replacement controls mirror observation."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import zlib

import pytest

from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline._commit_patch import inverse_address_resolver
from astrid.sdk.timeline._doc_patch import inverse_changes
from astrid.sdk.timeline._step_undo import inverse_stages, select_inverse_records
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle

# Extracted without changing records or identities from 058-provider-entry.json,
# SHA256 9056bdaa7926863a3c44d24a0303a6b3c790067623842f13e7f85bb2357974cd,
# in archive 502266a70f0d1a3cb21d354e9cce01245acb39ea014649fe528039e997de1e8c.
_CAPTURE = (
    'c-rlKNps^!lI~wg<8`R4*n!Z!&EDzFOi#7FLzzq#Yl_nn$tERrrlsk>KMw%hK?)>5NEH22l_G#xeUC5Pe-GSb{2YEh4{m=CMm{`E'
    'C(mK*e%=2w{2JU2M&s8on}?fO@RPwX{W)>wc{CZP*X_jrO8+<0(S3TY_?Zp|*385G^u<1o%6IQa9-4aoIll2yo6MQ0D3L-~X)=*H'
    't)0t_%Dl`6)yK2&I+`!PG%wE&li6te<7R4RVf=ivcznZ&ic;!~WLXHFQ7wW9HaD4!mgh8RsdB3V3qsl4$0(qyEapylnR&}1^yel|'
    'PiE}rgWJDgCbJ*&G^4e_rw7f(e)l#UOr~k3qo?pV8i()I>On7a_Yj`U#wyLjV{owS<Cn+B;b8e>KnHJk!-1dux}Uwk-_bnI!*K9u'
    'Y<PsH{}}tpw?zlvf*(CY>rY|!Bf!Hr3Xd?8;b1zO{4X?KE?-83<wWO-8zH<BHYc7MN)5?!Z-q_6o4m|iNHc#O8Te%S3s!tQfC<f?'
    '!?awRYN75YGd~)`px{RW11-8(O>_jrG=^CX2Jeh{YM$K#v;j>#o3!eG-KBXiUaM$kP^@W~RWw%&V)XoH^-|dkv@-bPmHtspdj7|I'
    '6aHh-$^4J!Kgt$UKgFicAHodUOM{%up#51ek5A=bB(*u0A|pU%f@a=muW0~Ev(z}JN%8i-Uc&qtnF5*O%uG>H+Wmu>C;HdHd6rA&'
    'BFmg}gzI1l6_kfOFcG7)xrmG_qG{ABkj!e%6!%uCToKMvlYZh}o?x8CW}lI~pmiUnVeAV%1Issx70;}VAT2>!wi&GI{gPj+H7}O)'
    '-j9A1-woyuhDmixHK$AqE`s4Oq~Jvky({WTBrqNpXl@+UjJuG<NSGlm5Rw_fa++&p3jVEUvBdz^RslZAZDtA3zxGTio$JVyk;-W='
    'qRU_$xwKpe?w!bTV>FdKD$NY#8K;AqUA;?0>l3XVn58fwr!r${P9Tu-=%oujGeEh7Xdwk>A?JZgNrQ?RqakQVh|Qhju;pY>byD`)'
    'Rikgi{Uer6u>gbzZxg9xq}D{^i8mlTxL}#0f}6mc3o$b?kx*{DkswubSXj?$N8Gy6LnF=|lbOghBZ^0@W%MzV$^yA$Kv0ev3A7fZ'
    'a9*;UXCdhU&Ip0!hys*B?TA}9dT7M(A4$2(0*NXZlM}-^v;ibfa%wejqbC}u-NamS>hhRb%Vb(7L0A^D+7Y*IG*R^YVe)*KZ%LFn'
    'g<e1w*_;JwZN?+Z33W_l!DK#@IRU0pkqfG2F1S_DJP{E_qjWZ?krem6a`L{;(%e@aS)cJ-Y6lXD*$9$K0q5|H2_qE9YE4M6fr04K'
    'nlVYZh(<-`t>rAUMkPK-4VIn6(c=@W@7tldpIux|&9BFa@rAya&%;7)n-@Qtl>c;(qe@en>9l<Bc{VD(a4%^v4m3MVqWB#C{4Z$L'
    'z(2@$QhxmBuW3@^=ILfWHq)Ewqj^pm^<nY|jfnJCu^dxc3}<GZlJ<fIpJ&GXGd(CiNUXDO0IYTZ;@APOlLx?S2aq2-0RH{}sxJOs'
    'b-};Ooi3)c(OUC#*}R)g=JP#0R3FuKA!}x_uZz)m4i0F8KA>H_+wODv*KVuTaxL2&l+NzpVKi84zWBIyljh;aQ)#@-(-v1>&jYZP'
    '|5iC6{|56fQTbowI~|hmVpu&N+}*8j?LU^ZwYl!4(jN}uZ1Pl*elhgdNzo&)5=*}=#`-$hSnB`Y!!r9?wZT`d(v4=HUgo2uxn3ua'
    'V1%VFWRl+@#^h9$?EGkcO%l<joq>N$7yq7xA0t??U+a?KH;^*?=ParlCSBWf2X+jQZf(;YsBa*E+NRs<*|!BLm$J4rC`*3$=V)Be'
    '(rEr-9t*-LyIyeNp63fOv*+>aWc92R_sd5MI@)}C_x1^_>(Z53>c>I`?=wn_1ZUQ3p9ShbG238(E~!(_Mj?bU;PU4V_D$wCD3)8|'
    'bOsy3VM`d1aZ@`k)L_zxlW`pVC?f>h`ryG#L6?_%Sh8)>kHf*(B!(Oi`t3je@861*UOZ)MPuaIW|KERZJmqUo`L{p+&)@&{x3m*)'
    '!}S3@gA*R^C*Z5XFv7g_cZ=OlLo(x6Q~Vaw`yAxOLm2&dfRzI31j#q@DgFY4UQ2`1k$Zlb)zDE97I5xm1{>lkzs|puVV?UWrzx;b'
    'A+vs%Q5hOoy<To9{C!hcLBm9>E|gLl1opF;Jr`||b@8?s%G-_=RsGPP5m&?&?k6!80vyITpMv>SOmb(mcPV^gz}HVxOHq+UYy?zJ'
    'n5D1&D*i4bBIIsU7{49hy3YOjw-Z7vH4&ioz4+>m!<vZr;E{kbc&Y@#Q+Nh_`)o?pzA0T`$dk$ALUMpDbZ>#NVN_+T>LXpD+A!IE'
    'a?dj7t+9+tsiJUTg@iowfodwCSCvUGh2dGA8|F*|9q%JEKs*Z6IhYmpY+)?u2uupDh)Rqo^DIaYvp3vnDFv)fq{_es2g;PpIB=>7'
    'G}B<#fQ};c$S@WMHi4x!5m+49Rar(E^MYDnP!F?Jtk@(fp?@Hxv<w+FJSKyRX<4Ue4}>W!%e7HXNy#!VjDp!^PDEmzGgNsSIoMQ;'
    'xfnF1G6t1_Z_1U=B6Aw#(@}GC3cOVXYKl0Ya}rEc4D3n{)|TK7OgSk$OoU1yBd1Y0kQ`A4>mm!8wC32kNt=0TV4VWVRSq0SU{Ans'
    'qcVWtA!%6+%r<A#dB#oRN9wubTI5OzaopUTv<}P=1q=iG5uji2&t;4bW{}6UST+;j;Zwl`(J)JJ+d*t|5DkLl0>h6Oe#G!&*^RUz'
    'iPhX{m@<exFzz`eE`oFt!fIeuAT!VdJW6K0hf!DujK`S<=}cowE20A_)lBD7=UC1d6(mvvL<ypoPz7=}%NhKGxS$DWUsyXO0uXDO'
    'U(P`=gK!Zbus3vW<*Jlj@I*<@!w@r1K-yUk#@!)u0UfKAlfcB8^1wchL<SlGbOQ`YI4v@wjCWu;g0;zwCKenk#(4DJMIS<2!>zs2'
    'Yq;bY*u@-{Mk&VvxP@S<3L9YdK151Eqy@sYwZy}2lK`%zg^oPzkIFoaZvT=4hjOr%g3NTr(>}6yIMl*M6UK7_hD{zBr&(}RgNo2G'
    'twl%*D`?QXGIwAl3kkx_M9INe_AGb0EtR*|R}GikGab3-)aM>JhI+z1F(PUcqa{`+r=2m88|sAv<5!A2M6jQ{$SDJs=s3vUfiZVE'
    'LH0w@!k#09wZVw`kk*lrsf#n1yPzN54gEAYqIj-i_npV~KRIz8j@EWUx|in<bwRqU8`5P*pRUn!9DVh0Iz`%_PJP;Aqpxzd)BWkq'
    'k)q^0;fEv6XJF-A+6ko<{MYcz5|U{`gR+#tB7(;zg$R);21Yf!13DCZH`qM|8<wg1^O;`H$j^LmyM55~_CZHI!%ur2dGG*AfW5YM'
    '`t<bjoDN$m;OX{R)Vsdf4SaFnpsGANc@1MTE)b`0$2opSonUq0ly072{T_t0wCNx#O{ttub*7bq<m1WHXlxRmI<4dwh;&x{^|pKB'
    'Rs1rZ$I0xeJit1<FUOw>mJM`#EZBwHdCalkurq3{3*H>2RxlusAlTaA&~a<qLEx%Kf^BzVyJiPnBF5$Im=ovLM{vELIA4COc-^H+'
    'nsDR%79>a*SC?(>M`N(lD=X%M2mS8h^a9+naXh^vpo+UC!Qa1{*~p9+b36SM8zbr+M#cIk%nk<qE@764t)9sePaV2*Q#(TFbTE>B'
    '5jy8MN2zi8VLajIw50z9YzC1vh^#?m4I*n0S%b(LMAjg(25nqK)*!M5ku`{{L1YagYY<t3$Qne}AhHILHHfT1WDO!~5LtuB8bsD0'
    'vIdbgh^#?m4Mx-vS%b(LMAjg(27FAgd?uyo^AO5~J~jV%pE;x!yHO<9o{k>FY-Lldb!}h9{xRIp_4L}a;F2lm*V1hN9k^!pvalx?'
    'Il;l^d>XmM5BE<KzwzrdO=$8m-@9ZcYT`Ml5Ep^~U62z?KqJ8HQ$AIq2fe4=dL8-Ag(?D>H?PIPQthrgBS3(G>=jxtOF+re+<TB4'
    'hJzT^gZ&BdtGw`)+dp@c=_(U{;*D8|YYE>A@`RlUqv>-QcPdk+SD!EQ!dHp-hXV&)B&nSuI0FVM{)Unp#{Z=HmZ@9DziTP~gOFR2'
    'ATQxHNykw!qyc4vBKi8hDi$eleY|12cz+)L1)Weiq;fT*qZV{4X}bZ@ZGg*MkO7QZCZI!-gBWm>c}qc?Sm83}fe1(d=1k$tKzUTk'
    'gD=5c(c=8&v8*@XT$rY1>&-_vRMVgVgY;h0z>Ftj80aGc4H0OFKtlu?BG3?lh6pr7pdq^a2sA{XAp#8%Xox^V1R5gH5P^mWG(?~w'
    '0u2#ph(JRG8Y0jTfrbb)M4%x84H0OFKtqhEBhV0mh6pr7py3+OutIcI-uLWBMgHZu<`1JH;I*<~ZyOn}x3|!|AI*TV2E)By?{n1l'
    'e!XxmHeV>sn%mndh*0yO^2B$aY<Qr%k5_i#>RB53`-`wS!rgD`re5>S_GDpWR@9WXse;=j{9LnvH6G0g`*z@yy<X1UB8S|mWL9`a'
    '&qZj+&Xce7b{fIG)6P2StzMnZLh<f!@M<1QkK+Ri;U43_zU}bMfms!DYnz)6uEKSfM}FHQVGb?v#Zb;VEv!4v`>EmW?HI9H$B4}Z'
    '=-#gzG4GCvaRI0P!(-3h#2L=sjyku_&v0(t-?71>3we(7MQB%_<@mj)!%NZr@Up)Xn$LMgdWm7jo{wKY-%-|As|8;y`{HU=!4lK;'
    '>pruMi&?8%MSHIi4HsJXp!(ex#vn#YlX>m4KphFz1_PW#opLq`@am0m$sd+f<~AsB+=<f}kPrtVVnoJXZ*&z$X4?zV+&SA`riB^P'
    'tkr$TlSj9@unZgQ8X8S#G@;RiMiUxMXf$2D(X^{hYl4Uk73-yh65pA1)7bG*M#|BF=4S10VA7VIl}2KtUAv?>-uk>t!|bFPZ#Gn6'
    'Z%%2c8C++hx22*vslUy(s=)SXBfs*1cbO()K6!jCcI>Vm=u@1uZ>((bO9lIZk5(x#=%B>VHcKM1xbeS>%V5$(C$$O2Sq-YedYN>o'
    'kr504CUcNKxzBkNk%U}D_(ta`L4zcoab194{QZ-u0K5khlkCkya&nG^Wdl9tS!j}sWH+fu$_C-@!Fiu7n75QfCHa}O<G*0g<3eZw'
    '52fTR%bXVlgN5lbYfO<}1ekvW=0N(9039q7J&eMU=}U&ocjG8L`qjG|l734nuN3uZ!r+n=cZ1dGLJ#%{8~zv|?}xz7KMxT60wB+Q'
    'f{+jKR%K`m+Z$c`n70=j?7i7_VVfQ915LFPO|{~swIKctS<l4wKS1W;+hSF|nfbRei^o3u2X@6h6rI}u91!+^X{JS-?72o=FTDSy'
    'DRX;f+~|rH@J9bRZuBQ+=!F(K0JpcN1-zaANpI)(M~3a;#qG%uZ`$Kcd%S7i;HG^Gv9JNszdhp(kd-*V%Q&@XEoa}u_Ls~0-owkg'
    '>39BJY=4brKhEV6?;q8#cpVmN<*9CGvba{)`JIdv{RVoQ-`}@B<GIv@__mR0p_n2(V*+TAfKDPLfZ*prXw8@;TtuTH^VV{fS)<s|'
    '2aByP;1`>6fbW-~o*hcX<T02$29w8N@)%4WgUMqsdCY0aV;Y->UjwjLSsJtO1nh9KlhNA1YP{-*sz+2kqUsS<kEnV?)z>>l@2a#T'
    '&)VCn>LTlmDfI0+;9zi9j<qcMHeV|(R<7-unub3nFJo^?y1%TdRhwwW!e|XinHMF}4%YSiY(?rGVCj-rOHhxSbm6mtZQj>FNs6pO'
    'XS>O{g#`urD(l3RCFs6aV_T8^^x@aMs2sKxKblTp)qfQZ<Ih4^?_62!ae<M@zY>v0nNv_SRzY7ZNNY15Sx%^9A`3wNnal}@PZhbK'
    'TIPaV1saBk2ntE*tTB;K{yi>Z?{$}q{zy*B;O+n`^yJ;NVYPEOTF7W$h0(wkq(B8Q1p}K7!x1@j2-HE6Y04PbcE(Z7xC>c~gc$;c'
    'B*_e6IoLJI^u=gB+gxoJ?i?l{GTmoly3*B53^yt-EJYyB)A+B0>RP+}SB`IE!8cfPIsRIvY;>5@ImdR8ez%@udml%yh@t%?hZ5s!'
    'N{kNbsl6{T=;;z;-+@}!5~GVu@?A)bGgd!6OJ^uCu1;dKFJt$<#9&8BjB^?OU8GCzL@1oESnOFkL!oeOLZO3l8}ADR{t=ExZx;;q'
    'NQ&P2?+@>W(&5UaLkCr&-j@#IBb<!huCBC`4(<Ez4_t`S;o2OE_BD~-mk#nw4n=Rbp4th3_Wk#V+(rR#i2|U4%YGgZ%C-bh`y{~0'
    '$du*t9tjD#<LX@N_J!Rqf;n0jzeVP_M%8f+XxBD(WFKlgo)7QUTmSvti$)1>Z4%%tn6qsOkbjEvsPh5QdLKuxmlIJcT!d68((ZiL'
    'YYeM2;&_n(Df|N{{OoramK2V+81NRum2%nnFEDh)WmkTM;ciK|#pUbSkLu!l+3WmaG(EL`{lJfAm2H1Q)%x16*K5_UUf3%4Uh|;J'
    'Iko#_BiGXI<JG<N)wAaHz7Kt~ugdq<H`|kijagkFPTS;W!4ye#{yL%(XVc=ECzQH6x_VXe5r0mL>N%&8S#5U36A12|cGgMnOO#02'
    ';hV!n8Fsk|OA+iZMX*Q0e39~MbprBm#fxLaW*s9o7odB;Zp6G;@fULwVQwPKO@z6LFgMZFSNvVpP5Qmay|ftc_t{37q?Ajb+?~DU'
    '3Y!(MI%8D2zxYXQRo~{$*)g?(8+&rcw|Yc1XEd77XhNe2jV3gj&}h1PqiI*2)&vp1V727~vu+wYKFUZrI?&v#{m#onf95#e`n+3L'
    '?CrNi@1nOZO7UXt-EL~{!lFzbUyB`szvd2xV(wuiNk|tYSXR&4SlQwicwP;E`SySgHcq#e1(S>O-0IMGK`BSMSZl?Xk7=vcv07_u'
    '#nr18*RlI2$;6J@-zyW-uI(PP1KW*Y8}I*}EQ0E{bx;Gk`(Mx9KWVIIj|E`;;ZItBc*Ax-rU+!uRTw*Fqc0Nty42$9XQR4K>$hS|'
    '6=SLxQ^lAn##Aw;iZNA;sbWmE>zHbv#a2J2{ON}j>-{|Cb1$G!Y@elDF;I_z`Yr?Y+fnyrp!au`Uk77+7~{K~7~im_#PACIe`2`1'
    'pqSp70povPApX~u9`_zK_D_$j8+v3%ht23YVp=_fok;tz)2EFGeU+e1_hFf@8F;CaV|Y}7M-_Nffkzd1RDnkocvP{gG54SQH#Oif'
    'g4`%5dZd?i67;g|c72+IiKeF9CFL%8|3I*3j=O@Bh>n<UKOVW!j{Su~vYTVJFL-cz6~}>%q&Xg%<DvPLb4e5YNn@4MW3=R~E__4j'
    'vA%xwkb|b?)hYmRiC%i+IOv*Hx*zP&c+HBi)7uBZ1csk9WYE-&!Q_L7fimz?5xleEF$uJdTs|OVe}wy|Nm*@akz+UI`+;@$_vHeV'
    'xSzTkp3k9*?{8&8PCt^}+lH&Fm~Ju+gW>)gg=<$K<0w4(LA`k2QhY0C@HDW{C80$dtPX-N(T+qr@&xeZ9^R_t>##j2b072eg3)|$'
    'c3ogM5BGsk&J&@WHCg#rdHV+cla%(i#j1QW^KXf*l9sGj{;(_Vq3HZjn6F3$m^g1&T_2fDpPN*en3>3PDlAu)QO3NW)(YzbQHm9{'
    'B&ampMOTz6%&6fpIR;G2Ixhnucb!7+ii#Y4(1KGWGYVLOIG=M8Oa#ps7-tgvKL;+H6dpDnRDq0~M&)usBWMH{S;(X{ozMbekv}OG'
    'xj!;=(mElOGD3l<3DOutoQ%-{vE|WeqHHEKr&KUOG!P_Vxy?OSIu|4tosc1-E)jK!sLKYZ%NAmxS#d3zSr$3CAWAtF7y*T&gbh?^'
    'A0nkW4}oxPE%C5sBsd!_SSUVHqcTsU+Ykpw0M^^UVcHv2{o>`%zK71SoG~g0Yzweio+Y<iQ<~)r{y|*OL?{BZt3-gwa;8JholC-B'
    'NXb564?8f1`o4v8_FeQ$NA5ZGxrbR(Pq-&WL~UZU#Ojm~j4_cL>V=~`lOhi>(9DaRGT8Eih~;CproDmC5!$SF#p|-HtvuCL>7MI$'
    'orfDy(GSkqnSSW6uJ=(HLWhe0RKsY}hIv$M$^pJ#hI)1=_3IqA?y?-7>21S+53=HgMe&>SatD^>wIIokSk(4L!j<9Hj#%6FW^}LT'
    'Hm!8m<%3oln}=WH>P2N~%)%3}!^uuYHwCO4K~(U5B;Jq2`;mA*67NUi{m6Y_$HOYE=fd@gxSvg-Z`T0_g91zVGWHX&>&oF3e^f}o'
    'WMjQGPsg8S&Rb&{mr_OHfYw8v`9L)lpmS9wy%dILd2X09v4~wW1B9S}Xn=TE;Goi}Hfd*mL1*K_vp{enf)f#(h~Pv7Cn7iz!HEb?'
    'L~tU46A_$<;6wx`A~+Gji3m<aa3X>e5uAwNL<A=yI1#~#2u?(BB7zeUoQU8=1ScXm5y6QFPDF4bf)f#(h~Pv7Cn7iz!HE|FPE2!g'
    'lc(wEG0axE3fC+L`!e>A;fAhb6K25jV2b%QFv35^e)4TG^lz~5&x>q{3*^XrJALBv*J+yR<fUMw>5F|Fxhf=E?CsC-jdw<Xmj%=('
    'v|yHi1xs`9Rn83ua@T_%A(WNf4&r-by$T_E^IGWU+9^obJZMo-B89NhWFm7~JC`S?Q09y8<_{_R=^!m2n9FHiE{vvy#l3)KR@Amw'
    '?C<IKV!7Qcm=y3$B67gMn7^UqhVehCzGdo`%kSVm{RbhpBmqqVxE{&dD2RgcK><h!PtytPvHNxZ&+x0D(=^3!lQ65rMos<~BxE_O'
    'j0nq#&J{O8cyNGo;;Eq^f3n=$!bMrUKM((cUZ@&Ub+??-QEM{`;10~6?;p(kA(4)yAUk!E58x)ky8;9UbS}Ws<2eb2Tc?!Nne`+C'
    '!;nN6n-#Fl#%b%SoSiR^#kv|0ztgmCO=*Y|6(uxIGLT%JQ7wW9HaD4!Hpy@;Rc;l?GNEklV-zrzEapylnR&}1^lr^O+@DJ8#m7Bj'
    'hMgHt#xT&wdp~h63wL%22mW;rWI2kkJ;l$}E0r4EaZwf!<3g6)`_Yf$yM=xL#X`Y$=ir8dsSc|va#%mG=}FRajDeysj%vmotWYG('
    'fbU93W(b2-(#jl=1ZQ!9mX24se}$wit&u~sY^}S8=P?NOkGQoVGdz!Vw132{jhNwiETjD+ZfzS4&th=hm7%7?d`lvTDd@#25P2*}'
    'Ycn2MPN-ud3kGDc%n2}+id;}FbHS~G=81?f8l|(r@S}qn{T<_M21$!J`u@3}U0kl$4936)1~xFTfq@MSY+ztx&x!AcTi2z3v(%5J'
    'W4y;GF%o=9uYDG%1I28E0lK74ISbyEPzF4|+`+!d+y=#ROPtPNLpW>+BQkDkN5tYF7UyHIIL)J5ts@g}#|L@p>YS|a#aDM6zFM8$'
    'k937<ME-CQ_pDry*^liwaS_366go?jE2lGm7!`v1*czO-w=gO{ngN4s+*fZ_Hh1sW3pu#?Ldo5?x0Rr;c~I%0-6tDzdH3;3ysVxT'
    'wV2;sgv}A|ep5H~ns2rzn`&1ri7=6D70WJ(H2j>VmrmR-tZv&?b=y<u9jkeFz4YtC;!70Y-jZ`yQ)qHqjAPBiQ%>4zRUX#I#`@T('
    '#`jnL-8KWidWqWR#_e%{TTEH-6;l>$o{HhU(LMJ@-;Qwrd*mxj3-j5C`{FLPziq`SdZb>=Ac7f0FoQ^g3?iL{-ak)XfVa05%KG;L'
    'MZN_3R%qM;tYoTq_}-+e&C>AgJC=ccc8PA3R`6fLGfPOO37GGeGT3s*b16iKOtEMqyyKMg-T<vBAg@gIipVZJ)w)|_w~d^y2FrwY'
    'Kid6h_peE*Cj7Q9g4H{}p+A8Bz}ZNC5wPr?s?kqCKLP!OtMwCh#Zk=>@hPPeKJ7vAJ9BP&G}m@P@PKfIOE_jr4xFv^xFy#B)LGk='
    'bC(c7X28?(uU|rBS@GiZQXyXmxU?Z&dMR?U=&fBbldq8&ti$Rn2qxPClf4zIIiEbfE|AA{mm7+p!GdnfzbX_ue6UIhQl?#Lnp<U&'
    'Hen|c9}UZVMm=}lN5zVpODQsv8z&se2sd8HoG5Cn62j|99r%x2NS;|~L{U@5-#?jZ!{5)>vNt`;$@wal4fL3^qDhXH-CQdvvxdK)'
    'jFTv=_V+-&JoB7#)-3af48C{`y-)2Jp0GB}tkdon4sGU5SEcRN?KML`bnC9g(fpXwmB>TrcVg*E^hf2}m#@M3HQd(o)euE5w#JLC'
    '4qq2|v1R7Y_cC_&T}8D0rFTrrjoGU(dlhD{!t7O;z3Sb|!e=gY*Fj2@uU6aR$U=8mJ`Br;VfipDABN?_uzVPn4?8v7a$4z5Ol*mX'
    'E&DDq+qC2wUQ0P+>Acnk$!FWgORT}(%}%z})V-9R{6uxwyIJ+Nw&zzVJ$b2DAiJ*D)>e(2pC0fE`X{}D-Z3e*)`aILMZCv~_gL{B'
    'YlC~NU5kdcR@hoq(YtbZWnIwuTCvJ<N!E-m_+#=i_NJt%Jt*mtHt%O<oXUs(MTYt=S$5B(*Kq&uw6JTBX!_t(WO~ADmmjQ^MZGB^'
    'G+Rh*=wzcL=64W<KZ;G5K+nGlcl~E^KClsHDa7a6gfFY^<L2%Z(SVQyeo<xgA(S_&FQQ`W11w#F`WJ+iHhxsxaWD}$W`hnEn3<yP'
    'z}bqf?Gjf-^6N`o(py>G_4{(<lYfuPC|uor?>~}bGPqk84bz8T^P+a=2jO$}@JH|C==Jopj-F5ud^JJPLG7CN1pz&iAm|Ozutxy&'
    ')_;GYb7cC<V*1LjJ(BA?NR9YD*Ry?Gzn7nv=X!??(jHeHIpRvxVEZzQ7r_v%i|ZjnT#JeTgF!hNG>CgIm2@OB$+8?A3a}-d78y~-'
    'yIgS~7H%}LzzvM?=)H?Rghm{}k1`A!0pz-}8aqI^wXzyJ=)XTGABu!)5D7-wAb|OEt2GT)SkQ}{5*HQtg~DpaK}&KN5RZ~suopSE'
    'PO0LA9@e3mk`$+lgncN)&O}0c3}HKw(7ylvz>+8uu0bSNAPT~GPN45RGETu&q&kDq!~`D*FiR0Mb5Z8bDkdb+)I`Y%jECh;Hxdc*'
    'L&HZMpt9R>ef$3VgP$YUUjx@e4<rMdk*MTGf_ezj8x;+ATFPW0M5>I=b)ZbijDw$PfKD`+lHgYp+U)d5eS~k&Gf1kdqowxa=oMg$'
    'V&XcC1D0!}oC1pwl!Q^qj&vdt3zi~P-bT<Nih*$uz>1JDsLb%dmCqte$c<+HLiQ2<LeBuUj>JR<<LDK?j$-0E#Duh&mj<*&AbD~a'
    'asrkD0qa2qZWT$&#em?*8Fij<lVA%emnhdFS4xORVj}+(Sd<QK4Rj+3+K;2xs}eQzbhl)v0@ykGG2h~zVow_}PrB1pPGJ5pDzLA|'
    'YK&A~bKpm_ilmM-OZR@gAj8cU7L~6nSaQvS>IUTQlZ_jJyN_2;+10b=Ri_Vqv#+A=);HUeg^gL^E)$s7&4PJeBsLB|r$rXy)AkD!'
    'Xm*o8^E7(L%(GoD{kpLD0tu0K<lL2Hg4`A3m_P7@EJ^h$50mv`vR+i<^~rkc%)qam)AbnR7E3GRy;dx(+^o**F%BS>P{tC<7gR!d'
    'D^7u>lJSZ;UNOf~$>%SXyi9-pdvS$+G2rj36#27Ys@v*&Yj8EIN^r(|L;amo?XP#=$oXopOlbF`-H&$v!GiA1Jb@44H}nV4A2=Jy'
    'F9MdmQ#JYt=qI3`aJ7EIt~jbWBK~rfa}Uh9>Cqfk#O=1GYSS$_aJGh(ZqKzVSlSj(%ki|lZE4$O0Z=SqyR{<y)T~|fhP7#bV=>w@'
    '7o&y6nmoQPkjHZe3uNw7+#Td)AwkNZ!GdnfzbX_ue6W0TKnL3}=H1H+K=UK$tIQKy{kDE<1mpY|=f^ld#`!VMk8ysC^JAPJ<NO%s'
    '$2dR6`7zFqaej>RW1Ju3{21rQI6ubuG0u;1evI=U73bd$+%H4$zpKImO;+|=OU|csEsg4IBK_@4T5yoN3nBkL6*k|8%fG?>u5M_y'
    'W`Er^TeGj0Y2=V$({Sak&hEW(PX;}N?YtSy<OWh7`q9^&UFQ9eRISP}c3wVfX4K%W9Fae+b@aBONxHc!6zsUdYH7jN<olV@&b218'
    '&y_>({X?Qp#B^7Xv9a|LR#T3<L=esJSiFNc1K!~%*j|1H*j|4Q=AsLE4onBMtIvWy><KU;+5<-V^5s7(F69!#N<BkfK3f#BGq?<='
    'xz257i8h%AicmV&ktqXqr1m1ZOmLB>T>gT4C$iiaO(l;?Gebf5(GKA<4UHHC=pjH40eT3~>qO%0M+<l1Zy`b+5%QlcGv<j`LOvfm'
    '-~}zbpmpI1m`}V832R&siw%#-FdO^Hx5bKl1JX!5_Y`J7!u`{vDEJje;nB~F+n)1hU{ilz8f(=h*qU3i&!J+zt!ilLN3wev_V<Da'
    '=58`AsejLf*F-(lyTzym^(PKn0mxMp^fV;Z<v{08Ses_?$nF;o1IC+R?zUUE_weRJx9(b?_ziXsk@<)yKt#a_$oxILRlz5Rh}d5u'
    '_V-DlUI6^>Z4(0f-&-TZ`FD)--zX-#%C0{cyyO1BzPN{C^1FeFRnVeH(4Sa)*I_xXP;Uo^C_8L|Xa9Kik7xgQ_K#=(c=o@21L<V7'
    'R`>-qDsAKU8|yQkOI;jY*vPa{u%kQ!2T>?Og(f7}z<3Z^GbRZa(WuD0wVY+vD0VRBf05XP+7++M)VI>oU&JjL4ps$@(NL|2;%pQz'
    'zVL`odV3#-Zb}K7Sr)l+UMa@{BQjVeYykP-L!=b6Lm*sROFT$r5}b`TX4FS&ROV^Crufnki|Uz<+;i%4FF>Pv!aXq}Y7?U+R=eCu'
    '#zbzY7Y>A+6nTh&W?tl!<ym%p(X1oZ);X3lMg;*H1cL~c3+P@`n&k}sL0r%T6dafWN(4|9oN2IeoJ$ZDAtmcOb~aqZeHsRF_F0b?'
    '8*Vvd$t)u*JG!?<WnJq{|94bVzZZO(GinF+=TSni9jCoDHxCh(R1bxyIDHnLpoNp&mevNzXWPdM1hsUulWjG1<DL8(K-P{{y{+xp'
    'oPQ7kdkzTfyL52msYY8ha(;S14Cp7tfObrZtu^8KNf9p+;YA|6NYvmWQP-lOtreE2=-E`J6#V;T>?hzjCRoSw&+(0SMt~~;8zi(~'
    'mVh-%bMIBo4F^T12a8WC&~)+?-<lWJ;Y39#bw)DSEYPJ|1QBd*G8t{6EiF}U6>OhSFz6VCcA#l<C%gpZ%;M75En^BMZOSj`Y+N|u'
    'h~q{aH{!Sv$Bj5{#Bn2z8*$u-<3=1e;<ypVjW}+^aU+f!aomXGMjSWdxDm&VIBvvoBaRz!+=$~w95>>)5yy=<Zp3jTjvH~@h~q{a'
    'H{!Sv$Bj5{#Bn2z8*$u-<3=1e;<ypV{rPd+>929Ju)q<Xr%+9S%YOU+da%%|'
)


@pytest.fixture
def captured():
    raw = zlib.decompress(base64.b85decode(_CAPTURE))
    assert hashlib.sha256(raw).hexdigest() == '16d87e20b1dc37be2246c8f9db0e61d3af12069c42acd03a980a9cd68a94c970'
    return json.loads(raw)


def replay(captured):
    history = captured['history']
    entry = history['items'][-1]
    documents = captured['documents']
    bundle = bundle_from_document(documents[entry['revision_id']])

    def boundary(source):
        return tuple(bundle_from_document(documents[revision]) for revision in (
            source['previous']['revision_id'], source['revision_id']))

    entries = {item['publication_id']: item for item in history['items']}
    resolver = inverse_address_resolver([entry], bundle, load_boundary=boundary,
                                        load_history=entries.__getitem__)
    records = select_inverse_records(AcceptedRecords(
        [entry], entry['records']['commands'], boundary), address_resolver=resolver)
    return bundle, records


def test_captured_config_root_redo_preserves_exact_history(captured):
    original = copy.deepcopy(captured)
    bundle, records = replay(captured)
    original_bundle = copy.deepcopy(bundle)
    entry = records.entries[0]
    record = records[0]
    assert record['derived_writes'] == []
    assert record['writes'][0]['path'] == ['config']
    assert 'clips' not in record['writes'][0]['before']['value']
    assert record['writes'][0]['after']['value']['clips'] == []
    assert len(record['writes']) == 10
    assert len(entry['records']['steps'][0]['patch']) == 9
    stages = list(inverse_stages(records))
    assert len(stages) == 1
    assert stages[0][2][0]['writes'] == record['writes']
    assert stages[0][2][0]['derived_writes'] == []
    assert stages[0][2][0]['_inverse_effects'] == inverse_changes(entry['records']['steps'][0]['patch'])
    request = captured['context']['request']
    assert request['base_revision_id'] == entry['revision_id']
    assert request['base_publication_id'] == entry['publication_id']
    plan = execute(bundle, request['commands'], inverse_records=records,
                   commit_request=request, evaluated_head=captured['history']['head'])
    sources = [source for source in records.load_boundary(entry)]
    assert document_from_bundle(plan.bundle, source_bundles=sources) == captured['expected_document']
    boundaries = [binding['boundary'] for binding in plan.records[0]['bindings']
                  if binding['key'] == 'inverse.stage']
    assert boundaries == [{'publication_id': entry['publication_id'], 'step_id': 'inverse',
                           'command_ordinals': [0], 'native_write_range': [0, 10],
                           'effect_range': [0, 9]}]
    assert plan.steps[0]['patch'] == inverse_changes(entry['records']['steps'][0]['patch'])
    assert plan.records[0]['derived_writes'] == []
    assert plan.records[0]['writes'] == [
        {**write, 'before': write['after'], 'after': write['before']}
        for write in reversed(record['writes'])]
    assert bundle == original_bundle
    assert captured == original


@pytest.mark.parametrize('damage', [
    'config-before', 'config-after', 'config-site', 'native-before', 'native-after',
    'native-order', 'effect-before-config', 'effect-after-config', 'effect-before-clips',
    'effect-after-clips', 'extra-mirror', 'native-range', 'effect-range', 'source',
    'step', 'ordinal', 'owner', 'digest', 'scope',
])
def test_captured_config_root_rejects_tampering_without_mutation(captured, damage):
    entry = captured['history']['items'][-1]
    record = entry['records']['commands'][0]
    config = record['writes'][0]
    clips = record['writes'][2]
    effect = entry['records']['steps'][0]['patch'][2]
    boundary = next(binding['boundary'] for binding in record['bindings'] if binding['key'] == 'inverse.stage')
    if damage == 'config-before':
        config['before']['value']['clips'] = []
    elif damage == 'config-after':
        config['after']['value']['clips'] = copy.deepcopy(clips['before']['value'])
    elif damage == 'config-site':
        config['path'] = ['unrelated']
    elif damage == 'native-before':
        clips['before']['value'].reverse()
    elif damage == 'native-after':
        clips['after']['value'] = copy.deepcopy(clips['before']['value'])
    elif damage == 'native-order':
        record['writes'][0], record['writes'][2] = record['writes'][2], record['writes'][0]
    elif damage.startswith('effect-') and damage not in ('effect-range',):
        side = 'before' if '-before-' in damage else 'value'
        image = effect[side]['value'] if side == 'before' else effect[side]
        if damage.endswith('config'):
            image['config']['app'] = {'foreign': True}
        else:
            image['clips'] = [] if side == 'before' else copy.deepcopy(clips['before']['value'])
    elif damage == 'extra-mirror':
        record['derived_writes'].append({'ref': 'parent', 'path': ['config', 'clips'],
                                         'cause': 0, 'before': copy.deepcopy(clips['before']),
                                         'after': copy.deepcopy(clips['after'])})
    elif damage == 'native-range':
        boundary['native_write_range'][0] = True
    elif damage == 'effect-range':
        boundary['effect_range'][1] -= 1
    elif damage == 'source':
        boundary['publication_id'] = 'foreign-publication'
    elif damage == 'step':
        boundary['step_id'] = 'foreign-step'
    elif damage == 'ordinal':
        boundary['command_ordinals'] = [True]
    elif damage in ('digest', 'scope'):
        document = captured['documents'][entry['previous']['revision_id']]
        if damage == 'digest':
            document['parent_revision']['content_digest'] = 'sha256:' + '0' * 64
        else:
            document['project_id'] = 'foreign-project'
    original = copy.deepcopy(captured)
    bundle = None
    original_bundle = None
    with pytest.raises((CommandError, ValueError)) as error:
        bundle, records = replay(captured)
        if damage == 'owner':
            bundle['placements'][0]['occurrence_id'] = 'foreign-occurrence'
        original_bundle = copy.deepcopy(bundle)
        request = captured['context']['request']
        execute(bundle, request['commands'], inverse_records=records,
                commit_request=request, evaluated_head=captured['history']['head'])
    if damage not in ('owner', 'digest', 'scope'):
        assert isinstance(error.value, CommandError)
        assert error.value.kind == 'history_unavailable'
    assert captured == original
    if original_bundle is not None:
        assert bundle == original_bundle


@pytest.fixture
def occurrence_state(captured):
    from astrid.sdk.timeline._command_state import State
    from astrid.sdk.timeline._root_inverse import normalize_root_inputs

    _, records = replay(captured)
    bundle, _ = records.load_boundary(records.entries[0])
    normalize_root_inputs(bundle, records)
    return State(bundle)


@pytest.mark.parametrize('order', [
    (0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0),
])
def test_occurrence_order_preserves_normalized_authored_rows(occurrence_state, order):
    from astrid.sdk.timeline._command_state import present

    state = occurrence_state
    original = copy.deepcopy(state.bundle)
    rows = copy.deepcopy(original['placements'])
    assert len(rows) == 3
    assert state.get('parent', ['occurrences']) == present(rows)
    target = present([rows[index] for index in order])
    expected = copy.deepcopy(original)
    expected['placements'] = copy.deepcopy(target['value'])
    state.set('parent', ['occurrences'], target)
    assert state.bundle == expected
    assert state.get('parent', ['occurrences']) == target
    target['value'][0]['shot_id'] = 'foreign-owner'
    observed = state.get('parent', ['occurrences'])
    observed['value'][0]['placement']['start_ms'] = -1
    assert state.bundle == expected


@pytest.mark.parametrize('damage', [
    'duplicate', 'missing', 'extra', 'membership', 'owner', 'physical-owner',
    'revision', 'shot-revision', 'field', 'nested-field', 'remove-field',
    'extra-field', 'bool-number', 'absent', 'not-list',
])
def test_occurrence_order_rejects_nonpermutations_without_mutation(occurrence_state, damage):
    from astrid.sdk.timeline._command_state import present

    state = occurrence_state
    target = present(list(reversed(state.bundle['placements'])))
    rows = target['value']
    if damage == 'duplicate':
        rows[0] = copy.deepcopy(rows[1])
    elif damage == 'missing':
        rows.pop()
    elif damage == 'extra':
        rows.append(copy.deepcopy(rows[0]))
    elif damage == 'membership':
        rows[0]['occurrence_id'] = 'foreign-occurrence'
    elif damage == 'owner':
        rows[0]['shot_id'] = 'foreign-owner'
    elif damage == 'physical-owner':
        state.bundle['placements'][0]['shot_id'] = 'foreign-physical-owner'
    elif damage == 'revision':
        rows[0]['revision_id'] = 'foreign-revision'
    elif damage == 'shot-revision':
        rows[0]['shot_revision_id'] = 'foreign-revision'
    elif damage == 'field':
        rows[0]['duration_ms'] += 1
    elif damage == 'nested-field':
        rows[0]['placement']['start_ms'] += 1
    elif damage == 'remove-field':
        rows[0].pop('gain')
    elif damage == 'extra-field':
        rows[0]['foreign'] = {}
    elif damage == 'bool-number':
        assert rows[0]['gain'] == 1
        rows[0]['gain'] = True
    elif damage == 'absent':
        target = {'state': 'absent'}
    elif damage == 'not-list':
        target = present({})
    original = copy.deepcopy(state.bundle)
    original_target = copy.deepcopy(target)
    with pytest.raises(CommandError) as error:
        state.set('parent', ['occurrences'], target)
    assert error.value.kind == 'identity_conflict'
    assert error.value.ref == 'parent'
    assert error.value.path == ['occurrences']
    assert state.bundle == original
    assert target == original_target
